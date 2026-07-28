# Private Todoist → Apple Calendar Sync

A small, self-hosted AWS sync for **all active Todoist projects** to an iCloud Calendar. It runs every five minutes and only manages the events it creates—your existing Apple Calendar events are untouched.

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

The deployment uses a 256 MB Lambda, an EventBridge rule every five minutes, DynamoDB on-demand for mappings, standard encrypted SSM Parameter Store values, and a private S3 artifact bucket. Typical use fits comfortably in Lambda's free tier; without it, expect roughly US$1–2/month, plus negligible storage/log costs.

## Prerequisites

- macOS, Linux, or WSL with Bash, Python 3, `pip`, `zip`, Git, and AWS CLI v2.
- An AWS account and credentials that can create CloudFormation, Lambda, IAM, DynamoDB, EventBridge, S3, and SSM resources in your chosen region.
- A Todoist [personal API token](https://app.todoist.com/app/settings/integrations/developer).
- An Apple Account with two-factor authentication and a freshly generated [app-specific password](https://account.apple.com/). This is the password for iCloud Calendar access, not your Apple Account password.

## Deploy

```bash
git clone https://github.com/Erscheinung/Todoist-Apple-Calendar-Sync.git
cd Todoist-Apple-Calendar-Sync
AWS_REGION=ap-south-1 scripts/deploy.sh
```

The script prompts without echoing the two secrets, builds Linux-compatible Lambda dependencies, creates a private artifact bucket if needed, deploys the CloudFormation stack, writes the encrypted parameters, and runs one test sync.

Choose an existing iCloud calendar name such as `Home`. The first sync can take a couple of minutes if you have many dated tasks.

## Configuration

| Prompt / environment variable | Default | Meaning |
| --- | --- | --- |
| `AWS_REGION` | `ap-south-1` | AWS region for all resources. |
| Calendar name | `Home` | Existing iCloud calendar to receive sync events. |
| `DISPLAY_TIMEZONE` | `Asia/Kolkata` | IANA timezone for Todoist floating times, such as `America/New_York` or `Europe/London`. |
| `ALARM_MINUTES_BEFORE` | `10` | Alert lead time for timed tasks. All-day tasks alert at 9:00 AM local time. |

For example:

```bash
AWS_REGION=eu-west-1 DISPLAY_TIMEZONE=Europe/London ALARM_MINUTES_BEFORE=15 scripts/deploy.sh
```

To replace a leaked or revoked token/password without redeploying, run:

```bash
AWS_REGION=ap-south-1 scripts/rotate-secrets.sh
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
