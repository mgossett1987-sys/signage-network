#!/usr/bin/env python3
"""Collect public Springfield, Missouri family calendars for the signage slide."""

import argparse
import base64
from datetime import date, datetime, time, timedelta
import html
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time as clock
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from icalendar import Calendar

LOCAL = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("events")
LIBRARY = "Springfield-Greene County Library"
PARKS = "Springfield-Greene County Park Board"
DEFAULT_TITLE = "Family Fun Around The Area"
EXCLUDED = re.compile(
    r"\b(cancelled|canceled|postponed|adults? only|"
    r"libraries closed|library closed|staff training|board meeting)\b|\b(?:21|18)\+", re.I
)


def sources():
    # Same public export used by the library calendar's Add to Calendar button.
    spec = {"feedType": "ical", "filters": {
        "location": ["all"], "ages": ["Kids & Families"],
        "types": ["all"], "tags": [], "term": "", "days": 30,
    }}
    data = base64.b64encode(json.dumps(spec).encode()).decode()
    return {
        LIBRARY: "https://thelibrary.libnet.info/feeds?data=" + quote(data, safe=""),
        PARKS: "https://parkboard.org/common/modules/iCalendar/iCalendar.aspx?catID=42&feed=calendar",
    }


def fetch(url):
    """Bounded, anonymous HTTPS requests; retry temporary network failures."""
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": "signage-network-family-events/1.0",
                "Accept": "text/calendar",
            })
            with urlopen(request, timeout=25) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ValueError("Calendar exceeds 2 MB limit")
            return body
        except (OSError, ValueError):
            if attempt == 2:
                raise
            clock.sleep(2 ** attempt)


def clean(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", str(value))).split()).strip(" -")


def in_window(start, now, days):
    if isinstance(start, datetime):
        return now <= start < now + timedelta(days=days)
    return now.date() <= start < now.date() + timedelta(days=days)


def parse_calendar(body, source, now, days):
    if not body.strip().startswith(b"BEGIN:VCALENDAR") or not body.strip().endswith(b"END:VCALENDAR"):
        raise ValueError("Incomplete or non-calendar response")
    calendar = Calendar.from_ical(body)
    result = []
    for event in calendar.walk("VEVENT"):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        # These feeds publish expanded occurrences. Fail closed if that changes;
        # silently accepting RRULE would omit or invent recurrence dates.
        if any(key in event for key in ("RRULE", "RDATE", "EXDATE")):
            raise ValueError("Source now requires recurrence expansion")
        if event.errors:
            raise ValueError(f"Invalid event: {event.errors}")
        title = clean(event.get("SUMMARY", ""))
        location = clean(event.get("LOCATION", ""))
        description = clean(event.get("DESCRIPTION", ""))
        if EXCLUDED.search(title + " " + description):
            continue
        start = event.decoded("DTSTART")
        if isinstance(start, datetime):
            start = (start.replace(tzinfo=LOCAL) if start.tzinfo is None else start.astimezone(LOCAL))
            event_date = start.date()
            event_time = start.strftime("%I:%M %p").lstrip("0")
        elif isinstance(start, date):
            event_date, event_time = start, "All Day"
        else:
            raise ValueError("Invalid event start date")
        if not in_window(start, now, days):
            continue
        if not title or not location:
            LOG.warning("Skipping event missing a title or location from %s", source)
            continue
        result.append({"title": title, "date": event_date.isoformat(),
                       "time": event_time, "location": location, "source": source})
    return result


def event_start(event):
    day = date.fromisoformat(event["date"])
    if event["time"] == "All Day":
        return day
    return datetime.combine(day, datetime.strptime(event["time"], "%I:%M %p").time(), LOCAL)


def select_events(events, now, days, limit):
    unique = {}
    for event in events:
        try:
            if not all(isinstance(event.get(key), str) and event[key].strip()
                       for key in ("title", "date", "time", "location")):
                continue
            start = event_start(event)
            if not in_window(start, now, days):
                continue
            key = (event["title"].casefold(), event["date"], event["time"], event["location"].casefold())
            unique.setdefault(key, event)
        except (ValueError, TypeError, KeyError):
            LOG.warning("Ignoring invalid cached event")

    def sort_key(event):
        start = event_start(event)
        if not isinstance(start, datetime):
            start = datetime.combine(start, time.min, LOCAL)
        return start, event["title"].casefold(), event["location"].casefold()

    return sorted(unique.values(), key=sort_key)[:limit]


def read_existing(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("Existing events.json has an invalid shape; refusing to overwrite")
    return data


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def update(path, now, days=30, limit=4, fetcher=fetch):
    existing = read_existing(path)
    collected, failures = [], []
    feeds = sources()
    for source, url in feeds.items():
        try:
            events = parse_calendar(fetcher(url), source, now, days)
            LOG.info("%s: %d upcoming events", source, len(events))
            collected.extend(events)
        except Exception as error:
            # Source isolation includes unexpected parser/schema changes.
            LOG.warning("%s failed: %s", source, error)
            failures.append(source)
    if len(failures) == len(feeds):
        LOG.error("All sources failed; existing file left untouched")
        return 1
    # Retain only attributable upcoming records from failed sources. A successful
    # source replaces its old records, so removals/cancellations are respected.
    collected.extend(event for event in existing.get("events", [])
                     if isinstance(event, dict) and event.get("source") in failures)
    events = select_events(collected, now, days, limit)
    if not events:
        LOG.error("No usable upcoming events; existing file left untouched (may be stale)")
        return 1
    title = existing.get("title") or DEFAULT_TITLE
    if existing.get("events") == events and existing.get("title") == title:
        LOG.info("No content changes; keeping existing updated timestamp")
        return 0
    atomic_write(path, {"title": title, "updated": now.isoformat(timespec="seconds"), "events": events})
    LOG.info("Wrote %d events to %s%s", len(events), path,
             " (partial source failure; see warnings)" if failures else "")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "events.json")
    parser.add_argument("--days", type=int, default=30, choices=range(1, 61), metavar="1-60")
    parser.add_argument("--limit", type=int, default=4, choices=range(1, 5), metavar="1-4")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        return update(args.output, datetime.now(LOCAL), args.days, args.limit)
    except Exception:
        LOG.exception("Update failed; no replacement file published")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
