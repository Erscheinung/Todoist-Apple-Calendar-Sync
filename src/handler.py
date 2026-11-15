"""One-way Todoist to iCloud (and optionally Google) Calendar synchronizer.

Only events with an X-TODOIST-TASK-ID property (iCloud) or a private
`todoistTaskId` extended property in a dedicated calendar (Google) are managed,
so existing calendar entries are never modified or removed.

The same function also answers HTTP GETs from its Function URL with a compact
week digest (week_digest.py) when a bearer secret is configured.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3

from google_calendar import FREEBUSY_SCOPE, GoogleCalendar, event_body as google_event_body, fingerprint as google_fingerprint
from ics_calendar import read_busy as ics_busy
from week_digest import build_digest, fit_budget

LOG = logging.getLogger()
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))

SSM = boto3.client("ssm")
_TABLE = None


def _table():
    # Lazy: the digest function has no table (and no permission to one).
    global _TABLE
    if _TABLE is None:
        _TABLE = boto3.resource("dynamodb").Table(os.environ["MAPPINGS_TABLE"])
    return _TABLE


# Never delete calendar events when a Todoist read looks implausible: an empty list while
# mappings exist, or more than this share of all mapped events in one run.
MAX_DELETE_SHARE = 0.5
MIN_GUARDED_DELETES = 6  # small clean-ups (finishing a few tasks) are never blocked
TODAY = lambda: datetime.now(timezone.utc)
DISPLAY_TIMEZONE = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "Asia/Kolkata"))
ALARM_MINUTES_BEFORE = int(os.getenv("ALARM_MINUTES_BEFORE", "10"))
# Optional sinks/features; each is off unless its settings are present.
APPLE_ENABLED = bool(os.getenv("APPLE_ACCOUNT_EMAIL"))
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
GOOGLE_SA_PARAMETER = os.getenv("GOOGLE_SERVICE_ACCOUNT_PARAMETER", "")
GOOGLE_BUSY_CALENDAR_IDS = [c for c in os.getenv("GOOGLE_BUSY_CALENDAR_IDS", "").split(",") if c.strip()]
ICS_URL_PARAMETER = os.getenv("ICS_URL_PARAMETER", "")
ICS_ALL_DAY_BUSY = os.getenv("ICS_ALL_DAY_BUSY", "false").lower() == "true"
DIGEST_TOKEN_PARAMETER = os.getenv("DIGEST_TOKEN_PARAMETER", "")
DIGEST_HOURS = os.getenv("DIGEST_HOURS", "08:00-21:00")
_digest_cache: tuple[float, dict] | None = None
_digest_secret: tuple[float, str] | None = None
SECRET_TTL_SECONDS = 300


def _secret(name: str) -> str:
    return SSM.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _todoist_tasks(token: str) -> list[dict]:
    tasks: list[dict] = []
    cursor = None
    while True:
        query = f"?{urlencode({'cursor': cursor})}" if cursor else ""
        request = Request(
            f"https://api.todoist.com/api/v1/tasks{query}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Todoist request failed with HTTP {exc.code}") from exc
        if isinstance(payload, list):  # compatibility with older Todoist endpoint responses
            tasks.extend(payload)
            return tasks
        tasks.extend(payload.get("results", []))
        cursor = payload.get("next_cursor")
        if not cursor:
            return tasks


def _calendar(username: str, password: str, calendar_name: str):
    import caldav  # only the sync path needs CalDAV

    client = caldav.DAVClient(url="https://caldav.icloud.com", username=username, password=password)
    calendars = client.principal().calendars()
    for calendar in calendars:
        if calendar.name == calendar_name:
            return calendar
    available = ", ".join(sorted(c.name for c in calendars))
    raise RuntimeError(f"iCloud calendar {calendar_name!r} was not found. Available: {available}")


def _ical_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    # RFC 5545 folds at 75 octets. Keeping ASCII metadata and UTF-8 task names
    # intact matters more here than aggressive folding; CalDAV accepts these lines.
    return line


def _event_ics(task: dict) -> str | None:
    due = task.get("due")
    if not due:
        return None
    task_id = str(task["id"])
    due_date = due["date"]
    is_all_day = "T" not in due_date
    uid = f"todoist-{task_id}@todoist-apple-calendar.local"
    now = TODAY().strftime("%Y%m%dT%H%M%SZ")
    description = f"Synced from Todoist\\nhttps://app.todoist.com/app/task/{task_id}"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Private Todoist Apple Calendar Sync//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"LAST-MODIFIED:{now}",
        f"SUMMARY:{_ical_escape(task['content'])}",
        f"DESCRIPTION:{_ical_escape(description)}",
        f"X-TODOIST-TASK-ID:{task_id}",
        f"X-TODOIST-PROJECT-ID:{task.get('project_id', '')}",
    ]
    if is_all_day:
        start = date.fromisoformat(due_date)
        lines.extend((f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{(start + timedelta(days=1)):%Y%m%d}"))
    else:
        start = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
        duration = task.get("duration") or {}
        amount = int(duration.get("amount", 60))
        unit = duration.get("unit", "minute")
        end = start + (timedelta(hours=amount) if unit == "hour" else timedelta(minutes=amount))
        if start.tzinfo is None:
            # Todoist floating times are wall-clock times in the user's timezone,
            # not UTC timestamps. The calendar is intentionally configured for IST.
            lines.extend((
                f"DTSTART;TZID={DISPLAY_TIMEZONE.key}:{start:%Y%m%dT%H%M%S}",
                f"DTEND;TZID={DISPLAY_TIMEZONE.key}:{end:%Y%m%dT%H%M%S}",
            ))
        else:
            lines.extend((f"DTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}", f"DTEND:{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"))
    # Apple Calendar recognizes DISPLAY VALARMs. A date-only task is alerted at
    # 09:00 IST on its due day; a timed task is alerted shortly before start.
    lines.extend(("BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ical_escape(task['content'])}"))
    if is_all_day:
        lines.append("TRIGGER;RELATED=START:PT9H")
    else:
        lines.append(f"TRIGGER;RELATED=START:-PT{ALARM_MINUTES_BEFORE}M")
    lines.extend(("END:VALARM", "END:VEVENT", "END:VCALENDAR", ""))
    return "\r\n".join(_fold(line) for line in lines)


def _fingerprint(ics: str) -> str:
    # DTSTAMP changes every run; exclude timestamps to avoid unnecessary writes.
    stable = "\n".join(line for line in ics.splitlines() if not line.startswith(("DTSTAMP:", "LAST-MODIFIED:")))
    return hashlib.sha256(stable.encode()).hexdigest()


def _mapping(task_id: str) -> dict | None:
    return _table().get_item(Key={"task_id": task_id}).get("Item")


def _save_mapping(task_id: str, event_url: str, fingerprint: str) -> None:
    # update_item (not put_item) so the iCloud and Google columns of one task never overwrite each other.
    _table().update_item(Key={"task_id": task_id}, UpdateExpression="SET event_url = :u, fingerprint = :f, updated_at = :t",
                      ExpressionAttributeValues={":u": event_url, ":f": fingerprint, ":t": TODAY().isoformat()})


def _save_google(task_id: str, event_id: str, fingerprint: str) -> None:
    _table().update_item(Key={"task_id": task_id}, UpdateExpression="SET google_event_id = :e, google_fingerprint = :f, updated_at = :t",
                      ExpressionAttributeValues={":e": event_id, ":f": fingerprint, ":t": TODAY().isoformat()})


def _google() -> GoogleCalendar | None:
    if not (GOOGLE_CALENDAR_ID and GOOGLE_SA_PARAMETER):
        return None
    return GoogleCalendar(_secret(GOOGLE_SA_PARAMETER), GOOGLE_CALENDAR_ID)


def _delete_event(calendar, event_url: str) -> None:
    try:
        calendar.event_by_url(event_url).delete()
    except Exception as exc:  # absent events are already in the desired state
        LOG.warning("Could not delete stale event %s: %s", event_url, exc)


def _hours() -> tuple[int, int]:
    lo, hi = DIGEST_HOURS.split("-")
    return tuple(int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in (lo, hi))  # type: ignore[return-value]


def _digest_token() -> str:
    """The bearer secret, read from SSM at most once per 5 minutes per container, so
    unauthenticated requests cannot drive SSM/KMS calls."""
    global _digest_secret
    import time

    if _digest_secret is None or _digest_secret[0] < time.time():
        _digest_secret = (time.time() + SECRET_TTL_SECONDS, _secret(DIGEST_TOKEN_PARAMETER))
    return _digest_secret[1]


def _digest_response(event) -> dict:
    """GET / on the Function URL: bearer-protected week digest (cached 60 s per warm container).
    Authentication happens before any other work; Todoist failing is a 503, never an empty week."""
    global _digest_cache
    import time

    http = (event.get("requestContext") or {}).get("http") or {}
    if not DIGEST_TOKEN_PARAMETER or http.get("method") != "GET" or (event.get("rawPath") or "/") != "/":
        return {"statusCode": 404, "body": ""}
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not hmac.compare_digest(headers.get("authorization", "").encode(), ("Bearer " + _digest_token()).encode()):
        return {"statusCode": 401, "body": ""}
    if _digest_cache and _digest_cache[0] > time.time():
        body = _digest_cache[1]
    else:
        now = TODAY()
        try:
            tasks = _todoist_tasks(_secret(os.environ["TODOIST_TOKEN_PARAMETER"]))
        except Exception as exc:  # no tasks is not the same as "a free week"
            LOG.warning("Digest: Todoist read failed: %s", type(exc).__name__)
            return {"statusCode": 503, "body": ""}
        busy, failed = {}, 0
        start = now.astimezone(DISPLAY_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
        if GOOGLE_BUSY_CALENDAR_IDS and GOOGLE_SA_PARAMETER:
            try:
                google = GoogleCalendar(_secret(GOOGLE_SA_PARAMETER), "", scope=FREEBUSY_SCOPE)
                busy = google.busy(GOOGLE_BUSY_CALENDAR_IDS, start, start + timedelta(days=7))
            except Exception as exc:
                LOG.warning("Digest: free/busy failed: %s", type(exc).__name__)
            failed = len(GOOGLE_BUSY_CALENDAR_IDS) - len(busy)
        if GOOGLE_BUSY_CALENDAR_IDS and not GOOGLE_SA_PARAMETER:
            failed += len(GOOGLE_BUSY_CALENDAR_IDS)
        if ICS_URL_PARAMETER:
            try:
                busy["ics"] = ics_busy(_secret(ICS_URL_PARAMETER), start, start + timedelta(days=7), DISPLAY_TIMEZONE, ICS_ALL_DAY_BUSY)
            except Exception as exc:
                # Do not log exception text: it may contain the credential-bearing URL.
                LOG.warning("Digest: private calendar read failed: %s", type(exc).__name__)
                failed += 1
        body = build_digest(tasks, busy, now, DISPLAY_TIMEZONE, hours=_hours(), busy_expected=len(GOOGLE_BUSY_CALENDAR_IDS) + int(bool(ICS_URL_PARAMETER)),
                            caveats=["calendarUnavailable"] if failed else [])
        body = fit_budget(body)
        _digest_cache = (time.time() + 60, body)
    return {"statusCode": 200, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, ensure_ascii=False, separators=(",", ":"))}


def digest_handler(event, context):
    """Entry point of the separate, read-only digest function behind the Function URL."""
    return _digest_response(event if isinstance(event, dict) else {})


def lambda_handler(event, context):
    """Scheduled sync only (the digest is served by digest_handler in its own function)."""
    return sync()


def sync():
    token = _secret(os.environ["TODOIST_TOKEN_PARAMETER"])
    calendar = None
    if APPLE_ENABLED:
        apple_password = _secret(os.environ["APPLE_PASSWORD_PARAMETER"])
        calendar = _calendar(os.environ["APPLE_ACCOUNT_EMAIL"], apple_password, os.environ["APPLE_CALENDAR_NAME"])
    google = _google()
    active_ids: set[str] = set()
    created = updated = deleted = skipped = 0
    g_written = g_deleted = 0
    timed = all_day = 0

    for task in _todoist_tasks(token):
        if not task.get("due"):
            skipped += 1
            continue
        if "T" in task["due"]["date"]:
            timed += 1
        else:
            all_day += 1
        task_id = str(task["id"])
        active_ids.add(task_id)
        current = _mapping(task_id)
        if google:
            body = google_event_body(task, DISPLAY_TIMEZONE, ALARM_MINUTES_BEFORE)
            fp = google_fingerprint(body)
            if not current or current.get("google_fingerprint") != fp:
                _save_google(task_id, google.upsert(body), fp)
                g_written += 1
        if calendar is None:
            continue
        ics = _event_ics(task)
        fingerprint = _fingerprint(ics)
        if current and current.get("fingerprint") == fingerprint:
            continue
        if current and current.get("event_url"):
            try:
                event_resource = calendar.event_by_url(current["event_url"])
                event_resource.data = ics
                event_resource.save()
                _save_mapping(task_id, current["event_url"], fingerprint)
                updated += 1
                continue
            except Exception as exc:
                LOG.warning("Recreating event for task %s after update failure: %s", task_id, exc)
        try:
            event_resource = calendar.save_event(ics)
        except Exception as create_error:
            # A previous invocation may have created the deterministic UID but
            # stopped before its DynamoDB mapping was saved. Reconcile it
            # instead of creating a duplicate or failing forever.
            try:
                event_resource = calendar.event_by_uid(f"todoist-{task_id}@todoist-apple-calendar.local")
                event_resource.data = ics
                event_resource.save()
            except Exception:
                raise create_error
        _save_mapping(task_id, str(event_resource.url), fingerprint)
        created += 1

    # Todoist's active-task endpoint omits completed and deleted tasks.
    projection = "task_id, event_url, google_event_id"
    scan = _table().scan(ProjectionExpression=projection)
    stale = scan.get("Items", [])
    while "LastEvaluatedKey" in scan:
        scan = _table().scan(ProjectionExpression=projection, ExclusiveStartKey=scan["LastEvaluatedKey"])
        stale.extend(scan.get("Items", []))
    doomed = [item for item in stale if item["task_id"] not in active_ids]
    if doomed and (not active_ids or (len(doomed) >= MIN_GUARDED_DELETES and len(doomed) > MAX_DELETE_SHARE * len(stale))):
        LOG.warning("Skipping deletions: %d of %d mapped events would go (Todoist returned %d dated tasks). "
                    "Run again; if Todoist really is empty, delete the mappings by hand.", len(doomed), len(stale), len(active_ids))
        doomed = []
    for item in doomed:
        if calendar is not None and item.get("event_url"):
            _delete_event(calendar, item["event_url"])
            deleted += 1
        if google and item.get("google_event_id"):
            google.delete(item["google_event_id"])
            g_deleted += 1
        _table().delete_item(Key={"task_id": item["task_id"]})

    result = {
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "google_written": g_written,
        "google_deleted": g_deleted,
        "eligible_timed": timed,
        "eligible_all_day": all_day,
        "skipped_without_due_date": skipped,
    }
    LOG.info("Sync finished: %s", result)
    return result
