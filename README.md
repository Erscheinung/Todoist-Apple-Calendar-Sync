# Private Todoist → Apple Calendar sync

This repository deploys a small AWS Lambda in `ap-south-1`. Every five minutes it copies every active Todoist task with a due date to the selected iCloud calendar. Date-only tasks are all-day events; timed tasks without a Todoist duration are one hour long.

It is deliberately one-way. It never alters ordinary Apple Calendar events—only entries containing `X-TODOIST-TASK-ID`, which it creates. Completing or deleting a synced Todoist task removes its managed calendar event.

## AWS resources and cost

- Lambda: 256 MB, 90-second timeout, one concurrent invocation
- EventBridge rule: every 5 minutes
- DynamoDB on-demand mapping table
- Two SecureString SSM parameters, encrypted with the AWS-managed key

At this scale, normal usage should be within Lambda and DynamoDB free allowances; Parameter Store standard SecureString parameters have no separate per-parameter cost. Network egress and CloudWatch log retention are the main small variables.

## Credentials

Never put secrets in Git. The deployment installs the Todoist API token and Apple app-specific password directly into SSM Parameter Store. Revoke an app password if it is ever exposed. The Apple app-specific password is for the Apple Account, not Todoist.

## Deploy

`scripts/build.sh` creates a Lambda package. Deployment is performed with the AWS CLI after an artifact bucket is created, using the values only through a hidden prompt. Required configuration:

- Region: `ap-south-1`
- Apple Account email
- iCloud calendar name, default `Home`
- Todoist API token
- Apple app-specific password

After deploy, invoke the Lambda once manually and inspect its CloudWatch log before relying on the scheduled rule.
