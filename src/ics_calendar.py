"""Private ICS feed to busy intervals. Feed URLs are credentials; never log them.

No calendar API, OAuth or provider write is used. Recurrences and exceptions are
expanded locally only over the requested horizon. Event text is discarded.
"""
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 5000
TIMEOUT = 5


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_busy(url, start, end, zone, all_day_busy=False, opener=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("invalid private feed URL")
    opener = opener or build_opener(NoRedirects())
    request = Request(url, headers={"Accept": "text/calendar"})
    with opener.open(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise ValueError("private feed unavailable")
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("private feed too large")
    return decode_busy(body, start, end, zone, all_day_busy)


def decode_busy(body, start, end, zone, all_day_busy=False):
    from icalendar import Calendar
    import recurring_ical_events

    if len(body) > MAX_BYTES or b"BEGIN:VCALENDAR" not in body:
        raise ValueError("invalid calendar feed")
    calendar = Calendar.from_ical(body)
    if calendar.name != "VCALENDAR":
        raise ValueError("invalid calendar feed")
    # Bound expansion input before the recurrence library runs. Lambda's timeout
    # remains the CPU boundary for adversarial recurrence rules.
    if len(calendar.walk("VEVENT")) > MAX_EVENTS:
        raise ValueError("too many calendar events")
    events = recurring_ical_events.of(calendar).between(start, end)
    if len(events) > MAX_EVENTS:
        raise ValueError("too many calendar occurrences")
    intervals = []
    for event in events:
        if str(event.get("STATUS", "")).upper() == "CANCELLED" or str(event.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        if event.get("X-TODOIST-TASK-ID") is not None:
            continue
        value = event.decoded("DTSTART")
        all_day = isinstance(value, date) and not isinstance(value, datetime)
        if all_day and not all_day_busy:
            continue
        finish = event.decoded("DTEND", None)
        if finish is None:
            duration = event.decoded("DURATION", None)
            if duration is None and not all_day:
                continue
            finish = value + (duration or timedelta(days=1))
        def instant(v):
            if isinstance(v, datetime):
                return v.replace(tzinfo=zone) if v.tzinfo is None else v
            return datetime.combine(v, time.min, tzinfo=zone)
        lo = max(instant(value).astimezone(timezone.utc), start.astimezone(timezone.utc))
        hi = min(instant(finish).astimezone(timezone.utc), end.astimezone(timezone.utc))
        if hi > lo:
            intervals.append((lo.isoformat(), hi.isoformat()))
    return intervals
