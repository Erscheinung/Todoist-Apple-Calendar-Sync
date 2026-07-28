"""One-way Todoist to iCloud Calendar synchronizer.

Only events with an X-TODOIST-TASK-ID property are managed, so existing
calendar entries are never modified or removed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
import caldav

LOG = logging.getLogger()
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))

TABLE = boto3.resource("dynamodb").Table(os.environ["MAPPINGS_TABLE"])
SSM = boto3.client("ssm")
TODAY = lambda: datetime.now(timezone.utc)


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
        lines.extend((f"DTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}", f"DTEND:{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"))
    lines.extend(("END:VEVENT", "END:VCALENDAR", ""))
    return "\r\n".join(_fold(line) for line in lines)


def _fingerprint(ics: str) -> str:
    # DTSTAMP changes every run; exclude timestamps to avoid unnecessary writes.
    stable = "\n".join(line for line in ics.splitlines() if not line.startswith(("DTSTAMP:", "LAST-MODIFIED:")))
    return hashlib.sha256(stable.encode()).hexdigest()


def _mapping(task_id: str) -> dict | None:
    return TABLE.get_item(Key={"task_id": task_id}).get("Item")


def _save_mapping(task_id: str, event_url: str, fingerprint: str) -> None:
    TABLE.put_item(Item={"task_id": task_id, "event_url": event_url, "fingerprint": fingerprint, "updated_at": TODAY().isoformat()})


def _delete_event(calendar, event_url: str) -> None:
    try:
        calendar.event_by_url(event_url).delete()
    except Exception as exc:  # absent events are already in the desired state
        LOG.warning("Could not delete stale event %s: %s", event_url, exc)


def lambda_handler(event, context):
    token = _secret(os.environ["TODOIST_TOKEN_PARAMETER"])
    apple_password = _secret(os.environ["APPLE_PASSWORD_PARAMETER"])
    apple_email = os.environ["APPLE_ACCOUNT_EMAIL"]
    calendar = _calendar(apple_email, apple_password, os.environ["APPLE_CALENDAR_NAME"])
    active_ids: set[str] = set()
    created = updated = deleted = skipped = 0

    for task in _todoist_tasks(token):
        ics = _event_ics(task)
        if not ics:
            skipped += 1
            continue
        task_id = str(task["id"])
        active_ids.add(task_id)
        fingerprint = _fingerprint(ics)
        current = _mapping(task_id)
        if current and current.get("fingerprint") == fingerprint:
            continue
        if current:
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
    scan = TABLE.scan(ProjectionExpression="task_id, event_url")
    stale = scan.get("Items", [])
    while "LastEvaluatedKey" in scan:
        scan = TABLE.scan(ProjectionExpression="task_id, event_url", ExclusiveStartKey=scan["LastEvaluatedKey"])
        stale.extend(scan.get("Items", []))
    for item in stale:
        if item["task_id"] not in active_ids:
            _delete_event(calendar, item["event_url"])
            TABLE.delete_item(Key={"task_id": item["task_id"]})
            deleted += 1

    result = {"created": created, "updated": updated, "deleted": deleted, "skipped_without_due_date": skipped}
    LOG.info("Sync finished: %s", result)
    return result
