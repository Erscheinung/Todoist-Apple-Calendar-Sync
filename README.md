# Private Todoist → Apple (and Google) Calendar Sync

A small, self-hosted AWS sync for **all active Todoist projects** to an iCloud Calendar. It runs every 15 minutes from 7:30 AM through 11:45 PM Asia/Kolkata and pauses overnight. It only manages the events it creates—your existing Apple Calendar events are untouched.

> This is an independent project; it is not created by, affiliated with, or supported by Doist or Apple.

## What it does

- Syncs active Todoist tasks with a due date from every accessible project.
- Creates all-day Apple Calendar events for date-only tasks.
- Uses each task's Todoist duration, or one hour when no duration is set.
- Treats Todoist floating times in your selected IANA timezone (for example, `Asia/Kolkata`). Fixed-timezone tasks retain their absolute time.
- Adds an Apple Calendar alert: 10 minutes before timed tasks and 9:00 AM on the due day for all-day tasks by default.
- Updates the event—including its alert—when a Todoist task's title, due date, time, or duration changes.
- Removes managed events when their Todoist task is completed, deleted, or loses its due date.

Tasks without a due date do not appear in a calendar. This sync is deliberately one-way: edit tasks in Todoist, not Apple Calendar.

## Architecture and approximate cost

The deployment uses a 256 MB Lambda, two EventBridge cron rules covering 7:30 AM–midnight Asia/Kolkata, DynamoDB on-demand for mappings, standard encrypted SSM Parameter Store values, and a private S3 artifact bucket. The daytime window produces 65 scheduled syncs per day (about 2,000 per 31-day month), compared with 8,928 monthly syncs for the former five-minute, 24-hour schedule. This also reduces the two per-invocation KMS decrypt requests for the SSM secrets to about 4,000 per 31-day month, normally below KMS's 20,000-request monthly free tier. Lambda, EventBridge, and DynamoDB usage should remain within their free or negligible-cost ranges for this workload; CloudWatch Logs and S3 storage are extra but typically small.

The schedule is intentionally expressed in UTC because EventBridge cron rules use UTC. For the default `Asia/Kolkata` deployment, `cron(0/15 2-17 ? * * *)` runs from 7:30 AM through 11:15 PM IST, and `cron(0,15 18 ? * * *)` adds the 11:30 PM and 11:45 PM runs. To change the frequency or pause window, edit the two `ScheduleExpression` values in `infra/template.yaml`, convert the desired local times to UTC, then redeploy with `scripts/deploy.sh`. If using a timezone other than Asia/Kolkata, adjust the cron hours yourself; EventBridge cron does not apply the `DISPLAY_TIMEZONE` parameter to schedules and does not automatically handle daylight-saving changes.

## Prerequisites

- macOS, Linux, or WSL with Bash, Python 3, `pip`, `zip`, Git, and AWS CLI v2.
- An AWS account and credentials that can create CloudFormation, Lambda, IAM, DynamoDB, EventBridge, S3, and SSM resources in your chosen region.
- A Todoist [personal API token](https://app.todoist.com/app/settings/integrations/developer).
- An Apple Account with two-factor authentication and a freshly generated [app-specific password](https://account.apple.com/). This is the password for iCloud Calendar access, not your Apple Account password.

## Deploy

```bash
git clone https://github.com/Erscheinung/Todoist-Apple-Calendar-Sync.git
cd Todoist-Apple-Calendar-Sync
scripts/deploy.sh --region ap-south-1
```

The script prompts without echoing the two secrets, builds Linux-compatible Lambda dependencies, creates a private artifact bucket if needed, deploys the CloudFormation stack, writes the encrypted parameters, and runs one test sync.

Choose an existing iCloud calendar name such as `Home`. The first sync can take a couple of minutes if you have many dated tasks.

## Configuration

| Prompt / environment variable | Default | Meaning |
| --- | --- | --- |
| `--region` / `AWS_REGION` | `ap-south-1` | AWS region for all resources. |
| `--stack-name` / `STACK_NAME` | `todoist-apple-calendar-sync` | CloudFormation stack name. |
| `--name` / `DEPLOYMENT_NAME` | `todoist-apple-calendar-sync` | Unique name prefix for the Lambda, role, and schedules. |
| `--namespace` / `PARAMETER_NAMESPACE` | `/todoist-apple-calendar` | SSM Parameter Store path for the two secrets. |
| Calendar name | `Home` | Existing iCloud calendar to receive sync events. |
| `DISPLAY_TIMEZONE` | `Asia/Kolkata` | IANA timezone for Todoist floating times, such as `America/New_York` or `Europe/London`. |
| `ALARM_MINUTES_BEFORE` | `10` | Alert lead time for timed tasks. All-day tasks alert at 9:00 AM local time. |

For example:

```bash
scripts/deploy.sh --region eu-west-1 --timezone Europe/London --alarm-minutes 15
```

To replace a leaked or revoked token/password without redeploying, run:

```bash
AWS_REGION=ap-south-1 scripts/rotate-secrets.sh
```

## Google Calendar (optional, no 7-day expiry)

The same Lambda can also write your dated Todoist tasks to one **dedicated Google calendar**. It authenticates with a **service account**, not your Google login. A service-account key does not expire. A user OAuth refresh token from a Google Cloud project left in *Testing* status stops working after 7 days.

1. In Google Cloud, create a project, enable the **Google Calendar API**, create a service account, and download a JSON key.
2. In Google Calendar, create a calendar such as "Todoist (synced)". Share it with the service account's email, with **Make changes to events**. Copy the calendar ID from its settings.
3. Optional, for free/busy in the week digest: share your other calendars with the same service account, with **See only free/busy**. A calendar that isn't shared is skipped, not an error.
4. Deploy with:

```bash
scripts/deploy.sh --google-calendar 'abc123@group.calendar.google.com' --google-sa-file ~/Downloads/sa-key.json \
  --google-busy 'primary-calendar-id,work-calendar-id' --week-digest   # add --no-icloud for Google only
```

The key is stored as an SSM SecureString (`<namespace>/google-service-account`) straight from the file. Secrets are never passed on a command line or printed. The sync uses the `calendar.events` scope. Events are marked "free" (tasks don't block your availability). Each event has a private `todoistTaskId` property and an ID derived from the task, so a retried run updates the event instead of duplicating it. Removing the calendar's share stops the sync immediately.

## Week digest URL (optional)

`--week-digest` adds a **separate, read-only function** (`<name>-digest`, handler `handler.digest_handler`) behind a Lambda **Function URL**. It returns one compact JSON document: the next 7 days of tasks, overdue tasks, busy blocks from shared calendars, and free minutes inside `DigestHours` (default 08:00–21:00). It's built for a watch or a single language-model prompt.

- **Authentication first.** Every request needs `Authorization: Bearer <secret>`. The secret is generated once into SSM (`<namespace>/digest-token`) and cached per container for 5 minutes, so a wrong or missing token costs at most one parameter read. A wrong token gets 401. Anything but `GET /` gets 404. There are no CORS headers, and responses are `Cache-Control: no-store`.
- **Least privilege.** The digest function's role can read only the Todoist token, the digest secret and the service-account key, and write its own logs. It has no DynamoDB access, no CalDAV password and no calendar writes. Timeout is 20 s, with reserved concurrency of 2. It uses the `calendar.freebusy` scope.
- **Honest failures.**
  - If Todoist fails, the URL returns **503** with an empty body, never an empty week.
  - If a calendar can't be read, `busySources < busyExpected`, `complete: false` and the caveat `calendarUnavailable` are set.
- **Size.** The JSON is kept under 12 KiB. Tasks are dropped from the far end of the week first and counted in `more` / `overdueMore`, with the caveat `trimmed`.
- **Revoking access.** The URL doesn't expire; rotate the secret instead. An S3 presigned URL can't do this, because presigned URLs last at most 7 days.

### Digest v1

```json
{"v": 1, "generatedAt": "2026-11-14T08:00:00Z", "tz": "Asia/Kolkata", "hours": ["08:00", "21:00"],
 "overdue": [{"t": "Title", "p": 1, "due": "2026-11-12"}], "overdueMore": 0,
 "days": [{"date": "2026-11-14", "tasks": [{"t": "Gym", "p": 2, "at": "18:00", "min": 60}], "busy": [["15:00", "16:00"]], "freeMin": 330, "more": 0}],
 "busySources": 1, "busyExpected": 1, "complete": true, "caveats": []}
```

| Field | Meaning |
| --- | --- |
| `t` | Task title, trimmed to 80 characters |
| `p` | Priority: 1 is urgent … 4 is normal |
| `at` | Local time, for timed tasks only |
| `min` | Todoist duration |
| `rec` | Present when the task is recurring |
| `busy` | Merged busy blocks, local `HH:MM` |
| `freeMin` | Free minutes inside your hours; today counts only the time still ahead |

- `busyExpected`, `complete`, `caveats` and `more` are optional additions, so v1 readers keep working.
- **Calendar event titles are never included** (free/busy only). Task titles are.
- A recurring task appears only at its next occurrence.

### Facts confirmed for this setup (2026-11-14)

- **AWS:** a function URL with `AuthType: NONE` needs a resource policy that allows **both** `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`, otherwise callers get 403. This applies to new URLs since October 2025. The template grants both. A direct (non-URL) invoke of the digest function returns 404, because it carries no HTTP request. Source: [Control access to Lambda function URLs](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html).
- **Google:** a calendar shared as **"See only free/busy"** with the service account is enough for `freeBusy.query`. Event details stay hidden. Writing events needs **"Make changes to events"** on the dedicated calendar. Source: [Calendar sharing](https://developers.google.com/calendar/concepts/sharing) and [Freebusy: query](https://developers.google.com/workspace/calendar/v3/reference/freebusy/query).

### Deletion guard (sync)

A read from Todoist sometimes looks implausible, such as:
- an empty list while events are mapped;
- one run that would remove more than half of the mapped events (and at least 6).

When that happens, the sync **skips deletions** and logs a warning instead of emptying your calendars. Small clean-ups after finishing a few tasks are never blocked.

```bash
curl -s -H "Authorization: Bearer $DIGEST_TOKEN" "$DIGEST_URL" | python3 -m json.tool | head
```

## Tests

```bash
python3 -m pip install cryptography   # only for the RS256 signing test
python3 -m unittest discover -s tests
```

## Verify and troubleshoot

Invoke the function and read its result:

```bash
aws lambda invoke --region "$AWS_REGION" \
  --function-name todoist-apple-calendar-sync \
  --invocation-type RequestResponse response.json
cat response.json
```

View logs:

```bash
aws logs tail /aws/lambda/todoist-apple-calendar-sync \
  --region "$AWS_REGION" --since 15m --follow
```

If iCloud returns `Unauthorized`, revoke the old app-specific password in your Apple Account, create a new one, then run `scripts/rotate-secrets.sh`. Do not put tokens, app-specific passwords, or Todoist iCal feed URLs in Git or issues.

## Security notes

Secrets are stored as encrypted SSM `SecureString` parameters and never belong in this repository. The Lambda role can only read the two parameters under its configured namespace and access its own DynamoDB mapping table. The calendar events contain a Todoist task ID so the sync can identify only events that it owns.


## Private ICS calendar for the shared Watch / nAI digest

The digest can read a private HTTPS iCalendar feed without a Google API project,
OAuth consent, service account or iCloud password. Its URL is a bearer credential:
store it only in SSM SecureString at `<namespace>/calendar-ics-url`. Never use the
public embed page or commit the private URL. The Watch and nAI receive only the
separate digest endpoint and digest token, never this feed URL.

Set `EnableWeekDigest=true`, `EnableIcsCalendar=true` and retain the existing sync
parameters. `IcsAllDayBusy=false` makes all-day events **informational by default**,
matching nAI's calendar policy. Set it to `true` if opaque all-day events should
reserve the whole local day (including 23/25-hour DST days). Transparent and
cancelled events never block time. Recurrences, exclusions and moved instances
are expanded for the seven-day horizon. Floating events use `DisplayTimezone`.
Events carrying `X-TODOIST-TASK-ID` are excluded to avoid counting mirrored work
twice; unrelated copies without this marker cannot be identified automatically.
Only busy intervals leave the adapter; event titles, notes, locations and IDs do not.

The GET has a five-second network timeout, a 2 MiB response limit and refuses
redirects. Source/occurrence counts are capped at 5,000; the Lambda timeout is the
CPU boundary for recurrence expansion. An unreadable/malformed/oversized feed is
partial coverage, **never an empty/free calendar**. A valid empty feed counts as
one successfully read calendar. Digest source time is its fetch time; an upstream
feed may itself lag calendar edits, so this is not real-time push sync.

### Update an existing stack without touching the calendar writer

Do **not** use the default deploy script for this update: it invokes the sync
function as a smoke check. Instead prepare a CloudFormation UPDATE change set:

1. Run `scripts/build.sh` locally. Upload its ZIP under a new artifact key in the
   existing artifact bucket. Set **DigestArtifactKey** to that key and keep
   **ArtifactKey** on its previous value. The existing sync Lambda then retains
   its code, environment, schedule and calendar target.
2. Put the private feed URL into `<namespace>/calendar-ics-url` as SecureString
   from a mode-0600 file using `--value file://<private-file>`. Generate a separate
   random digest token in a mode-0600 file and store it at
   `<namespace>/digest-token`; preserve an existing token when present. Neither
   value should appear in shell arguments, history or logs.
3. Create an UPDATE change set with this template, `CAPABILITY_NAMED_IAM`,
   `UsePreviousValue=true` for all existing parameters, and only these overrides:
   `EnableWeekDigest=true`, `EnableIcsCalendar=true`, `DigestArtifactKey=<new-key>`
   and the chosen `IcsAllDayBusy` policy. Leave Google busy IDs empty for an
   ICS-only setup. Inspect the change set before executing it.
4. Expected resources: a new digest role, digest Lambda (256 MiB, concurrency 2),
   HTTPS Function URL and its two invoke permissions. The role can read Todoist,
   digest-token and private-feed SSM parameters plus write its own logs; it has no
   DynamoDB, Apple password or Google service-account access in ICS-only mode.
   SSM parameters and the ZIP upload are separate from CloudFormation.
5. After executing the approved change set, make a **GET of the digest URL only**
   using its bearer token; inspect version/coverage/counts without logging task
   titles. Never invoke the sync Lambda during this validation.
6. Configure nAI's private digest URL/secret files and ClaudeWatch's existing
   Keychain provisioning with the same digest endpoint/token. Those clients need
   no Google/iCloud credentials. Live Watch behavior remains a device check.

Rollback: disable the digest (`EnableWeekDigest=false`, `EnableIcsCalendar=false`)
while retaining previous sync parameters, or restore the previous digest artifact
key. SecureString parameters and artifact objects require separate cleanup if
no longer wanted. No deployed change is implied by these instructions.
