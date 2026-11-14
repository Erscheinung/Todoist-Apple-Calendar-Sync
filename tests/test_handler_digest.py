"""Digest function and sync guards, with AWS, CalDAV and the network stubbed out (offline)."""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class FakeSSM:
    values = {"/ns/todoist-token": "todo", "/ns/digest-token": "s3cret", "/ns/google-service-account": json.dumps({"client_email": "a@b", "private_key": "x"})}

    def __init__(self):
        self.reads = []

    def get_parameter(self, Name, WithDecryption):
        self.reads.append(Name)
        return {"Parameter": {"Value": self.values[Name]}}


class FakeTable:
    def __init__(self, items):
        self.items = {i["task_id"]: dict(i) for i in items}

    def get_item(self, Key):
        return {"Item": self.items.get(Key["task_id"])} if Key["task_id"] in self.items else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        self.items.setdefault(Key["task_id"], {"task_id": Key["task_id"]})

    def scan(self, **kw):
        return {"Items": list(self.items.values())}

    def delete_item(self, Key):
        self.items.pop(Key["task_id"], None)


def load_handler(env=None):
    ssm = FakeSSM()
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_: ssm
    boto3.resource = lambda *_: (_ for _ in ()).throw(AssertionError("the digest path must not touch DynamoDB"))
    sys.modules["boto3"] = boto3
    base = {"TODOIST_TOKEN_PARAMETER": "/ns/todoist-token", "DIGEST_TOKEN_PARAMETER": "/ns/digest-token"}
    with mock.patch.dict(os.environ, {**base, **(env or {})}, clear=False):
        os.environ.pop("MAPPINGS_TABLE", None)  # the digest function has no table
        sys.modules.pop("handler", None)
        import handler
    handler.os.environ.update(base)
    return handler, ssm


def event(auth=None, method="GET", path="/"):
    return {"rawPath": path, "requestContext": {"http": {"method": method}}, "headers": {"Authorization": auth} if auth else {}}


TASKS = [{"id": "1", "content": "Pay bill", "priority": 4, "due": {"date": "2026-11-14"}}]


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.h, self.ssm = load_handler()
        self.h._digest_cache = None
        self.h._digest_secret = None
        self.h._todoist_tasks = lambda token: TASKS

    def test_imports_without_a_table_and_routes(self):
        self.assertEqual(self.h.digest_handler(event("Bearer s3cret", "POST"), None)["statusCode"], 404)
        self.assertEqual(self.h.digest_handler(event("Bearer s3cret", path="/admin"), None)["statusCode"], 404)
        r = self.h.digest_handler(event("Bearer s3cret"), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(r["headers"]["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", r["headers"])

    def test_bad_requests_read_the_secret_at_most_once(self):
        for auth in (None, "Bearer nope", "Bearer still-nope"):
            self.assertEqual(self.h.digest_handler(event(auth), None)["statusCode"], 401)
        self.assertEqual(self.ssm.reads.count("/ns/digest-token"), 1)
        self.assertNotIn("/ns/todoist-token", self.ssm.reads, "no other work before authentication")

    def test_todoist_failure_is_503_not_an_empty_week(self):
        def boom(token):
            raise RuntimeError("Todoist request failed with HTTP 503")
        self.h._todoist_tasks = boom
        r = self.h.digest_handler(event("Bearer s3cret"), None)
        self.assertEqual((r["statusCode"], r["body"]), (503, ""))

    def test_calendar_failure_is_a_caveat(self):
        self.h.GOOGLE_BUSY_CALENDAR_IDS = ["cal-a", "cal-b"]
        self.h.GOOGLE_SA_PARAMETER = "/ns/google-service-account"

        class G:
            def __init__(self, *a, **k):
                pass

            def busy(self, ids, start, end):
                return {"cal-a": []}  # cal-b errored (not shared)
        self.h.GoogleCalendar = G
        body = json.loads(self.h.digest_handler(event("Bearer s3cret"), None)["body"])
        self.assertEqual((body["busyExpected"], body["busySources"], body["complete"]), (2, 1, False))
        self.assertEqual(body["caveats"], ["calendarUnavailable"])

    def test_complete_when_nothing_failed(self):
        body = json.loads(self.h.digest_handler(event("Bearer s3cret"), None)["body"])
        self.assertTrue(body["complete"])
        self.assertEqual((body["busyExpected"], body["caveats"]), (0, []))


class SyncGuardTests(unittest.TestCase):
    def run_sync(self, tasks, mapped):
        h, _ = load_handler({"APPLE_ACCOUNT_EMAIL": ""})
        h.APPLE_ENABLED = False
        table = FakeTable([{"task_id": str(i), "event_url": f"u{i}"} for i in range(mapped)])
        h._TABLE = table
        h._todoist_tasks = lambda token: tasks
        h._google = lambda: None
        with mock.patch.dict(os.environ, {"TODOIST_TOKEN_PARAMETER": "/ns/todoist-token"}):
            h.sync()
        return table

    def test_empty_read_deletes_nothing(self):
        self.assertEqual(len(self.run_sync([], 8).items), 8)

    def test_more_than_half_at_once_deletes_nothing(self):
        live = [{"id": str(i), "content": "t", "due": {"date": "2026-11-14"}} for i in range(3)]
        self.assertEqual(len(self.run_sync(live, 10).items), 10)

    def test_small_cleanup_still_happens(self):
        live = [{"id": str(i), "content": "t", "due": {"date": "2026-11-14"}} for i in range(8)]
        self.assertEqual(sorted(self.run_sync(live, 10).items), [str(i) for i in range(8)])


if __name__ == "__main__":
    unittest.main()
