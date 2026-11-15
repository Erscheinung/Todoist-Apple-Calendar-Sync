"""Synthetic ICS only: no private feed, provider call or credentials."""
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ics_calendar import decode_busy, read_busy, MAX_BYTES, NoRedirects


def feed(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Fixture//EN\r\n" + "".join(
        "BEGIN:VEVENT\r\nUID:" + str(i) + "\r\nDTSTAMP:20261001T000000Z\r\n" + event + "\r\nEND:VEVENT\r\n"
        for i, event in enumerate(events)) + "END:VCALENDAR\r\n").encode()


class IcsTests(unittest.TestCase):
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 8, tzinfo=timezone.utc)
    zone = ZoneInfo("America/New_York")

    def parse(self, *events, all_day=False):
        return decode_busy(feed(*events), self.start, self.end, self.zone, all_day)

    def test_recurrence_exclusion_and_dst(self):
        spans = self.parse("DTSTART;TZID=America/New_York:20261031T090000\r\nDTEND;TZID=America/New_York:20261031T100000\r\nRRULE:FREQ=DAILY;COUNT=4\r\nEXDATE;TZID=America/New_York:20261102T090000")
        self.assertEqual(spans, [("2026-11-01T14:00:00+00:00", "2026-11-01T15:00:00+00:00"), ("2026-11-03T14:00:00+00:00", "2026-11-03T15:00:00+00:00")])

    def test_transparent_cancelled_and_todoist_mirror_ignored(self):
        base = "DTSTART:20261102T100000Z\r\nDTEND:20261102T110000Z"
        self.assertEqual(self.parse(base + "\r\nTRANSP:TRANSPARENT", base + "\r\nSTATUS:CANCELLED", base + "\r\nX-TODOIST-TASK-ID:fixture"), [])

    def test_all_day_informational_by_default_optional_busy_dst(self):
        event = "DTSTART;VALUE=DATE:20261101\r\nDTEND;VALUE=DATE:20261102"
        self.assertEqual(self.parse(event), [])
        self.assertEqual(self.parse(event, all_day=True), [("2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00")])

    def test_floating_cross_midnight_duration_and_no_text(self):
        spans = self.parse("DTSTART:20261102T233000\r\nDURATION:PT2H\r\nSUMMARY:Private title\r\nDESCRIPTION:Private notes")
        self.assertEqual(spans, [("2026-11-03T04:30:00+00:00", "2026-11-03T06:30:00+00:00")])
        self.assertNotIn("Private", str(spans))

    def test_fetch_bounds_https_and_no_redirect(self):
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def open(self, request, timeout):
                self.timeout = timeout
                return Response(feed())
        opener = Opener()
        self.assertEqual(read_busy("https://calendar.example/fixture.ics", self.start, self.end, self.zone, opener=opener), [])
        self.assertEqual(opener.timeout, 5)
        for url in ["http://calendar.example/fixture.ics", "https://user:pass@calendar.example/x", "https://calendar.example/x#fragment"]:
            with self.assertRaises(ValueError):
                read_busy(url, self.start, self.end, self.zone, opener=opener)
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.example"))
        with self.assertRaises(ValueError):
            decode_busy(b"x" * (MAX_BYTES + 1), self.start, self.end, self.zone)
        with self.assertRaises(ValueError):
            decode_busy(b"not a calendar", self.start, self.end, self.zone)


if __name__ == "__main__":
    unittest.main()
