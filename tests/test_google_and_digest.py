"""Offline tests: python3 -m unittest discover -s tests  (needs `cryptography` for the JWT test only)."""
import base64
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google_calendar import GoogleCalendar, event_body, event_id, fingerprint, service_account_assertion  # noqa: E402
from week_digest import build_digest  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def b64json(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class EventBodyTests(unittest.TestCase):
    def test_floating_time_keeps_wall_clock_and_zone(self):
        body = event_body({"id": "42", "content": "Gym", "due": {"date": "2026-11-14T18:30:00"}, "duration": {"amount": 75, "unit": "minute"}}, IST, 10)
        self.assertEqual(body["start"], {"dateTime": "2026-11-14T18:30:00", "timeZone": "Asia/Kolkata"})
        self.assertEqual(body["end"]["dateTime"], "2026-11-14T19:45:00")
        self.assertEqual(body["extendedProperties"]["private"]["todoistTaskId"], "42")
        self.assertEqual(body["transparency"], "transparent")

    def test_all_day_and_no_due(self):
        body = event_body({"id": "7", "content": "Pay bill", "due": {"date": "2026-11-15"}}, IST, 10)
        self.assertEqual(body["start"], {"date": "2026-11-15"})
        self.assertEqual(body["end"], {"date": "2026-11-16"})
        self.assertIsNone(event_body({"id": "8", "content": "x"}, IST, 10))

    def test_event_id_is_stable_and_valid(self):
        a, b = event_id("123"), event_id("123")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[a-v0-9]{5,1024}$")
        self.assertNotEqual(a, event_id("124"))

    def test_fingerprint_changes_with_content(self):
        t = {"id": "1", "content": "A", "due": {"date": "2026-11-15"}}
        self.assertNotEqual(fingerprint(event_body(t, IST, 10)), fingerprint(event_body({**t, "content": "B"}, IST, 10)))


class AuthTests(unittest.TestCase):
    def test_assertion_claims(self):
        info = {"client_email": "sync@example.iam.gserviceaccount.com", "private_key": "unused", "private_key_id": "k1"}
        jwt = service_account_assertion(info, now=1000, signer=lambda key, msg: b"sig")
        header, claims, sig = jwt.split(".")
        self.assertEqual(b64json(header)["alg"], "RS256")
        c = b64json(claims)
        self.assertEqual(c["iss"], info["client_email"])
        self.assertEqual(c["exp"] - c["iat"], 3600)
        self.assertIn("calendar.events", c["scope"])

    def test_real_rs256_signature_verifies(self):
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError:
            self.skipTest("cryptography not installed")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        jwt = service_account_assertion({"client_email": "a@b", "private_key": pem}, now=1)
        signing_input, sig = jwt.rsplit(".", 1)
        key.public_key().verify(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)), signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())


class ClientTests(unittest.TestCase):
    def make(self, responses):
        calls = []

        def http(req):
            calls.append((req.get_method(), req.full_url))
            if req.full_url.startswith("https://oauth2"):
                return 200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
            return responses.pop(0)

        cal = GoogleCalendar(json.dumps({"client_email": "a@b", "private_key": "x"}), "cal@group.calendar.google.com", http=http, signer=lambda k, m: b"s")
        return cal, calls

    def test_upsert_updates_then_creates_when_missing(self):
        cal, calls = self.make([(404, b""), (200, b"{}")])
        cal.upsert({"id": "abcde", "summary": "x"})
        self.assertEqual([m for m, _ in calls], ["POST", "PUT", "POST"])  # token, update (404), insert
        self.assertIn("cal%40group.calendar.google.com", calls[1][1])

    def test_delete_tolerates_gone(self):
        cal, _ = self.make([(410, b"")])
        cal.delete("abcde")

    def test_busy_skips_calendars_with_errors(self):
        payload = {"calendars": {"me": {"busy": [{"start": "2026-11-14T04:00:00Z", "end": "2026-11-14T05:30:00Z"}]}, "other": {"errors": [{"reason": "notFound"}]}}}
        cal, _ = self.make([(200, json.dumps(payload).encode())])
        busy = cal.busy(["me", "other"], datetime(2026, 11, 14, tzinfo=timezone.utc), datetime(2026, 11, 21, tzinfo=timezone.utc))
        self.assertEqual(list(busy), ["me"])


class DigestTests(unittest.TestCase):
    def test_week_digest_shape(self):
        now = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)  # 13:30 IST
        tasks = [
            {"id": "1", "content": "Pay bill", "priority": 4, "due": {"date": "2026-11-14"}},
            {"id": "2", "content": "Gym", "priority": 1, "due": {"date": "2026-11-15T18:00:00"}, "duration": {"amount": 60, "unit": "minute"}},
            {"id": "3", "content": "Old thing", "priority": 3, "due": {"date": "2026-11-10"}},
            {"id": "4", "content": "Far away", "priority": 1, "due": {"date": "2026-12-25"}},
            {"id": "5", "content": "No date", "priority": 1},
        ]
        busy = {"me": [("2026-11-14T09:30:00Z", "2026-11-14T10:30:00Z")]}  # 15:00–16:00 IST
        d = build_digest(tasks, busy, now, IST)
        self.assertEqual(len(d["days"]), 7)
        today = d["days"][0]
        self.assertEqual(today["date"], "2026-11-14")
        self.assertEqual(today["tasks"], [{"t": "Pay bill", "p": 1}])
        self.assertEqual(today["busy"], [["15:00", "16:00"]])
        self.assertEqual(today["freeMin"], (21 * 60 - (13 * 60 + 30)) - 60)
        self.assertEqual(d["days"][1]["tasks"], [{"t": "Gym", "p": 4, "at": "18:00", "min": 60}])
        self.assertEqual(d["overdue"], [{"t": "Old thing", "p": 2, "due": "2026-11-10"}])
        self.assertLess(len(json.dumps(d)), 3000)


if __name__ == "__main__":
    unittest.main()


class BudgetTests(unittest.TestCase):
    def test_500_tasks_fit_the_budget(self):
        from week_digest import BUDGET_BYTES, fit_budget
        now = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)
        tasks = [{"id": str(i), "content": f"Task number {i} with a fairly long title for size", "priority": 1 + i % 4,
                  "due": {"date": f"2026-11-{14 + i % 7:02d}" if i % 5 else "2026-11-01"}} for i in range(500)]
        d = fit_budget(build_digest(tasks, {}, now, IST))
        self.assertLessEqual(len(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()), BUDGET_BYTES)
        self.assertIn("trimmed", d["caveats"])
        self.assertFalse(d["complete"])
        kept = sum(len(x["tasks"]) + x.get("more", 0) for x in d["days"]) + len(d["overdue"]) + d["overdueMore"]
        self.assertEqual(kept, 500, "every dropped task is counted")
        self.assertGreater(len(d["days"][0]["tasks"]), 0, "the near end of the week is kept first")

    def test_busy_block_across_midnight_and_dst_zone(self):
        now = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
        d = build_digest([], {"me": [("2026-11-14T17:30:00Z", "2026-11-14T19:30:00Z")]}, now, IST)  # 23:00–01:00 IST
        self.assertEqual(d["days"][0]["busy"], [["23:00", "24:00"]])
        self.assertEqual(d["days"][1]["busy"], [["00:00", "01:00"]])
        ny = ZoneInfo("America/New_York")  # 2026-11-01 is the US fall-back day
        d = build_digest([{"id": "1", "content": "x", "priority": 1, "due": {"date": "2026-11-01T09:00:00Z"}}], {}, datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc), ny)
        self.assertEqual(d["days"][0]["date"], "2026-11-01")
        self.assertEqual(d["days"][0]["tasks"][0]["at"], "04:00")
