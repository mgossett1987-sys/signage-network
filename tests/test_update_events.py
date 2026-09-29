"""Offline regression tests; fixtures are synthetic, not advertised real events."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_events as app

NOW = datetime(2026, 9, 29, 12, tzinfo=app.LOCAL)


def feed(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(
        "BEGIN:VEVENT\r\n" + event + "\r\nEND:VEVENT\r\n" for event in events
    ) + "END:VCALENDAR\r\n").encode()


def item(title="Family Craft", start="20260930T150000Z", extra=""):
    return f"UID:test\r\nSUMMARY:{title}\r\nDTSTART:{start}\r\nLOCATION:Library Center\r\n{extra}"


def row(source=app.LIBRARY, title="Saved Event", day="2026-09-30", at="9:00 AM"):
    return {"title": title, "date": day, "time": at, "location": "Library Center", "source": source}


class ParserTests(unittest.TestCase):
    def parse(self, body):
        return app.parse_calendar(body, app.LIBRARY, NOW, 30)

    def test_utc_converts_to_central_and_unfolds_text(self):
        event = item(title="Crafts\\, Songs and\r\n  Stories")
        result = self.parse(feed(event))[0]
        self.assertEqual(result["time"], "10:00 AM")
        self.assertEqual(result["title"], "Crafts, Songs and Stories")

    def test_local_tzid_and_floating_time(self):
        for start in ("DTSTART;TZID=America/Chicago:20260930T090000", "DTSTART:20260930T090000"):
            event = item().replace("DTSTART:20260930T150000Z", start)
            self.assertEqual(self.parse(feed(event))[0]["time"], "9:00 AM")

    def test_dst_uses_central_standard_time_in_november(self):
        now = datetime(2026, 10, 31, 12, tzinfo=app.LOCAL)
        result = app.parse_calendar(feed(item(start="20261102T160000Z")), app.LIBRARY, now, 30)
        self.assertEqual(result[0]["time"], "10:00 AM")

    def test_all_day_today(self):
        event = item().replace("DTSTART:20260930T150000Z", "DTSTART;VALUE=DATE:20260929")
        self.assertEqual(self.parse(feed(event))[0]["time"], "All Day")

    def test_past_started_today_and_outside_window_removed(self):
        self.assertEqual(self.parse(feed(item(start="20260929T160000Z"),
                                         item(start="20261030T150000Z"))), [])

    def test_cancellations_closures_and_adult_events_excluded(self):
        self.assertEqual(self.parse(feed(item(extra="STATUS:CANCELLED"),
            item(title="Libraries Closed"), item(title="Adults only craft"), item(title="18+ Craft"),
            item(title="Postponed: Family Craft"))), [])

    def test_missing_location_skipped(self):
        self.assertEqual(self.parse(feed(item().replace("LOCATION:Library Center", "LOCATION:"))), [])

    def test_html_and_truncated_feed_rejected(self):
        for body in (b"<html>maintenance</html>", feed(item())[:-20]):
            with self.assertRaises(ValueError):
                self.parse(body)

    def test_malformed_dates_and_new_recurrence_format_fail_closed(self):
        for event in (item(start="nonsense"), item(extra="RRULE:FREQ=DAILY")):
            with self.assertRaises((ValueError, KeyError)):
                self.parse(feed(event))

    def test_sort_deduplicate_and_cap(self):
        rows = [row(title=str(i), at=f"{i}:00 PM") for i in range(5, 0, -1)]
        rows += [rows[0], row(title="All-day", at="All Day"), row(day="2026-09-28")]
        result = app.select_events(rows, NOW, 30, 4)
        self.assertEqual([r["time"] for r in result], ["All Day", "1:00 PM", "2:00 PM", "3:00 PM"])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.json"
        self.initial = {"title": "Family Fun Around The Area", "updated": "old",
                        "events": [row(), row(source=app.PARKS, title="Saved Park Event")]}
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")
        self.original = self.path.read_bytes()

    def test_total_outage_leaves_file_byte_identical(self):
        def fail(url):
            raise OSError("offline")
        self.assertEqual(app.update(self.path, NOW, fetcher=fail), 1)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_zero_results_preserves_file_and_reports_failure(self):
        self.assertEqual(app.update(self.path, NOW, fetcher=lambda url: feed()), 1)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_partial_outage_preserves_only_failed_sources_upcoming_rows(self):
        def partial(url):
            if "parkboard" in url:
                raise OSError("timeout")
            return feed(item())
        self.assertEqual(app.update(self.path, NOW, fetcher=partial), 0)
        data = json.loads(self.path.read_text())
        self.assertEqual([r["title"] for r in data["events"]], ["Saved Park Event", "Family Craft"])
        self.assertEqual(data["title"], self.initial["title"])

    def test_successful_source_cancellation_removes_old_row(self):
        def collect(url):
            return feed(item(extra="STATUS:CANCELLED")) if "parkboard" in url else feed(item())
        app.update(self.path, NOW, fetcher=collect)
        self.assertEqual([r["title"] for r in json.loads(self.path.read_text())["events"]], ["Family Craft"])

    def test_unchanged_content_does_not_touch_timestamp_or_bytes(self):
        fetcher = lambda url: feed(item())
        app.update(self.path, NOW, fetcher=fetcher)
        before = self.path.read_bytes()
        app.update(self.path, NOW.replace(hour=13), fetcher=fetcher)
        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replace_failure_leaves_original_and_cleans_temp(self):
        with patch.object(app.os, "replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                app.update(self.path, NOW, fetcher=lambda url: feed(item()))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_existing_file_is_not_overwritten(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            app.update(self.path, NOW, fetcher=lambda url: feed(item()))
        self.assertEqual(self.path.read_text(), "broken")

    def test_failed_source_expired_cache_is_not_republished(self):
        self.initial["events"][1]["date"] = "2026-09-01"
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")
        def partial(url):
            if "parkboard" in url:
                raise OSError("offline")
            return feed(item())
        app.update(self.path, NOW, fetcher=partial)
        self.assertEqual(len(json.loads(self.path.read_text())["events"]), 1)


if __name__ == "__main__":
    unittest.main()
