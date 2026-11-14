"""Compact week digest for a watch or a language model.

One small JSON document (typically 1–3 KB) with the next N days of Todoist tasks,
overdue tasks, and busy time from Google calendars shared with the sync's
service account. It is built to be pasted into a single model prompt, so it
keeps only what answers "how does my week look?": titles (trimmed), priority,
time, duration, busy blocks and free minutes inside the owner's hours.

Pure functions: no AWS, no network. handler.py serves it from a Lambda
Function URL behind a bearer secret.
"""
from __future__ import annotations

import json

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MAX_TITLE = 80
MAX_PER_DAY = 40


def _prio(task: dict) -> int:
    # Todoist API priority 4 = "p1" (urgent) … 1 = "p4" (normal).
    return 5 - int(task.get("priority", 1) or 1)


def _minutes(task: dict) -> int | None:
    d = task.get("duration") or {}
    if not d.get("amount"):
        return None
    return int(d["amount"]) * (1440 if d.get("unit") == "day" else 1)


def _local_due(task: dict, tz: ZoneInfo) -> tuple[date, str | None] | None:
    due = (task.get("due") or {}).get("date")
    if not due:
        return None
    if "T" not in due:
        return date.fromisoformat(due), None
    dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.date(), dt.strftime("%H:%M")


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def build_digest(tasks: list[dict], busy: dict[str, list[tuple[str, str]]], now: datetime, tz: ZoneInfo,
                 days: int = 7, hours: tuple[int, int] = (8 * 60, 21 * 60), busy_expected: int | None = None,
                 caveats: list[str] | None = None) -> dict:
    today = now.astimezone(tz).date()
    horizon = [today + timedelta(days=i) for i in range(days)]
    week = {d: {"date": d.isoformat(), "tasks": [], "busy": [], "freeMin": 0} for d in horizon}
    overdue: list[dict] = []

    for t in tasks:
        due = _local_due(t, tz)
        if not due or t.get("checked"):
            continue
        d, at = due
        item = {"t": t["content"][:MAX_TITLE], "p": _prio(t)}
        if at:
            item["at"] = at
        if (m := _minutes(t)) is not None:
            item["min"] = m
        if t.get("due", {}).get("is_recurring"):
            item["rec"] = True
        if d < today:
            overdue.append({**item, "due": d.isoformat()})
        elif d in week:
            week[d]["tasks"].append(item)

    # Busy minutes per local day (all calendars merged), clipped to the day.
    per_day: dict[date, list[tuple[int, int]]] = {d: [] for d in horizon}
    for blocks in busy.values():
        for s, e in blocks:
            start = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz)
            end = datetime.fromisoformat(e.replace("Z", "+00:00")).astimezone(tz)
            d = start.date()
            while d <= end.date():
                if d in per_day:
                    lo = start.hour * 60 + start.minute if d == start.date() else 0
                    hi = end.hour * 60 + end.minute if d == end.date() else 1440
                    if hi > lo:
                        per_day[d].append((lo, hi))
                d += timedelta(days=1)

    now_m = now.astimezone(tz).hour * 60 + now.astimezone(tz).minute
    for d in horizon:
        spans = _merge(per_day[d])
        week[d]["busy"] = [[_hm(s), _hm(e)] for s, e in spans]
        lo, hi = hours
        if d == today:
            lo = max(lo, now_m)
        free = max(0, hi - lo) - sum(max(0, min(e, hi) - max(s, lo)) for s, e in spans)
        week[d]["freeMin"] = max(0, free)
        ts = sorted(week[d]["tasks"], key=lambda x: (x.get("at") or "99", x["p"], x["t"]))
        if len(ts) > MAX_PER_DAY:
            week[d]["more"] = len(ts) - MAX_PER_DAY
            ts = ts[:MAX_PER_DAY]
        week[d]["tasks"] = ts

    overdue.sort(key=lambda x: (x["p"], x["due"], x["t"]))
    return {
        "v": 1,
        "generatedAt": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tz": tz.key,
        "hours": [_hm(hours[0]), _hm(hours[1])],
        "overdue": overdue[:MAX_PER_DAY],
        "overdueMore": max(0, len(overdue) - MAX_PER_DAY),
        "days": [week[d] for d in horizon],
        "busySources": len(busy),
        # Optional (v1-compatible) honesty fields: how many calendars were asked for, and
        # whether every source answered. A missing calendar is a caveat, never "free time".
        "busyExpected": len(busy) if busy_expected is None else busy_expected,
        "complete": not caveats,
        "caveats": sorted(set(caveats or [])),
    }


BUDGET_BYTES = 12 * 1024


def _size(d: dict) -> int:
    return len(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode())


def fit_budget(d: dict, budget: int = BUDGET_BYTES) -> dict:
    """Keep the digest under `budget` bytes of JSON: drop tasks from the far end of the
    week first (the last task of the latest day with tasks), then from the overdue list,
    counting what was dropped in `more` / `overdueMore` and adding the caveat `trimmed`."""
    if _size(d) <= budget:
        return d
    d = json.loads(json.dumps(d))
    while _size(d) > budget:
        day = next((x for x in reversed(d["days"]) if x["tasks"]), None)
        if day is not None:
            day["tasks"].pop()
            day["more"] = day.get("more", 0) + 1
        elif d["overdue"]:
            d["overdue"].pop()
            d["overdueMore"] = d.get("overdueMore", 0) + 1
        else:
            break
    d["complete"] = False
    d["caveats"] = sorted(set(d.get("caveats", [])) | {"trimmed"})
    return d
