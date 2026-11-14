"""Optional Google Calendar sink, authenticated with a service account.

Why a service account: a user OAuth refresh token from a Google Cloud project
left in "Testing" publishing status expires after 7 days, so a scheduled
Lambda would silently stop. A service account key does not expire. Share one
dedicated calendar with the service account's email ("Make changes to
events"), and pass that calendar's ID. The sync touches nothing else.

Only events carrying the private extended property `todoistTaskId` are
managed, and event IDs are derived from the Todoist task ID, so a retry after
a lost DynamoDB write updates the same event instead of creating a duplicate.

Standard library plus `cryptography` (for RS256 signing of the token request).
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
# Least privilege per function: the sync writes events in its one shared calendar; the
# digest only reads free/busy of calendars shared with "See only free/busy".
EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
FREEBUSY_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_rs256(private_key_pem: str, message: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def service_account_assertion(info: dict, now: int | None = None, signer: Callable[[str, bytes], bytes] = sign_rs256, scope: str = EVENTS_SCOPE) -> str:
    """The signed JWT a service account trades for a 1-hour access token."""
    now = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT", "kid": info.get("private_key_id", "")}
    claims = {"iss": info["client_email"], "scope": scope, "aud": info.get("token_uri", TOKEN_URL), "iat": now, "exp": now + 3600}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    return f"{signing_input}.{_b64url(signer(info['private_key'], signing_input.encode()))}"


def event_id(task_id: str) -> str:
    # Google event IDs: base32hex characters (a-v, 0-9), 5-1024 long.
    digest = hashlib.sha256(f"todoist:{task_id}".encode()).digest()
    return base64.b32hexencode(digest).decode().lower().rstrip("=")[:40]


def event_body(task: dict, display_tz: ZoneInfo, alarm_minutes: int) -> dict | None:
    """Google Calendar event for a Todoist task with a due date (None otherwise)."""
    due = task.get("due")
    if not due:
        return None
    task_id = str(task["id"])
    body: dict = {
        "id": event_id(task_id),
        "summary": task["content"],
        "description": f"Synced from Todoist\nhttps://app.todoist.com/app/task/{task_id}",
        "extendedProperties": {"private": {"todoistTaskId": task_id, "todoistProjectId": str(task.get("project_id", ""))}},
        "transparency": "transparent",  # a task is not a meeting: don't block free/busy for others
    }
    due_date = due["date"]
    if "T" not in due_date:
        start = date.fromisoformat(due_date)
        body["start"] = {"date": start.isoformat()}
        body["end"] = {"date": (start + timedelta(days=1)).isoformat()}
        # Google can't alert "at 09:00 on the day" for all-day events (reminders are minutes before
        # 00:00), so all-day tasks use the calendar's own default notifications.
        body["reminders"] = {"useDefault": True}
        return body
    start = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
    duration = task.get("duration") or {}
    amount = int(duration.get("amount", 60))
    end = start + (timedelta(days=amount) if duration.get("unit") == "day" else timedelta(minutes=amount))
    if start.tzinfo is None:  # Todoist floating time = wall clock in the owner's zone
        body["start"] = {"dateTime": start.isoformat(), "timeZone": display_tz.key}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": display_tz.key}
    else:
        body["start"] = {"dateTime": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
        body["end"] = {"dateTime": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": alarm_minutes}]}
    return body


def fingerprint(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class GoogleCalendar:
    """Tiny Calendar v3 client. `http` is injectable for tests."""

    def __init__(self, service_account_json: str, calendar_id: str, http: Callable[[Request], tuple[int, bytes]] | None = None,
                 signer: Callable[[str, bytes], bytes] = sign_rs256, scope: str = EVENTS_SCOPE):
        self.info = json.loads(service_account_json)
        self.calendar_id = calendar_id
        self._http = http or _urlopen
        self._signer = signer
        self._token: tuple[str, float] | None = None
        self._scope = scope

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        assertion = service_account_assertion(self.info, signer=self._signer, scope=self._scope)
        data = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}).encode()
        status, raw = self._http(Request(self.info.get("token_uri", TOKEN_URL), data=data, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"}))
        if status != 200:
            raise RuntimeError(f"Google token request failed with HTTP {status}")
        payload = json.loads(raw)
        self._token = (payload["access_token"], time.time() + int(payload.get("expires_in", 3600)))
        return self._token[0]

    def _call(self, method: str, path: str, body: dict | None = None, ok=(200,)) -> tuple[int, dict]:
        req = Request(f"{API}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"})
        status, raw = self._http(req)
        if status not in ok:
            raise RuntimeError(f"Google Calendar {method} {path.split('?')[0]} failed with HTTP {status}")
        return status, (json.loads(raw) if raw else {})

    def _events(self) -> str:
        return f"/calendars/{quote(self.calendar_id, safe='')}/events"

    def upsert(self, body: dict) -> str:
        """Create or replace the event with body['id']; returns the event ID."""
        status, _ = self._call("PUT", f"{self._events()}/{body['id']}", body, ok=(200, 404))
        if status == 404:
            status, _ = self._call("POST", self._events(), body, ok=(200, 409))
            if status == 409:  # deleted earlier (ID reserved by a cancelled event): restore it via update
                self._call("PUT", f"{self._events()}/{body['id']}", {**body, "status": "confirmed"})
        return body["id"]

    def delete(self, event_id_: str) -> None:
        self._call("DELETE", f"{self._events()}/{event_id_}", ok=(200, 204, 404, 410))

    def busy(self, calendar_ids: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[str, str]]]:
        """freeBusy for calendars shared with the service account (others come back as errors and are skipped)."""
        if not calendar_ids:
            return {}
        _, payload = self._call("POST", "/freeBusy", {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                                                       "items": [{"id": c} for c in calendar_ids]})
        out: dict[str, list[tuple[str, str]]] = {}
        for cid, cal in payload.get("calendars", {}).items():
            if cal.get("errors"):
                continue
            out[cid] = [(b["start"], b["end"]) for b in cal.get("busy", [])]
        return out


def _urlopen(req: Request) -> tuple[int, bytes]:
    try:
        with urlopen(req, timeout=25) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read() or b""
