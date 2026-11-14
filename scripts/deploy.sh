#!/usr/bin/env bash
set -euo pipefail

region="${AWS_REGION:-ap-south-1}"
# Secrets go to SSM from a 0600 temp file (file://), never as a command-line argument visible in `ps`.
put_secret() {
  _f="$(mktemp)"; chmod 600 "$_f"; printf '%s' "$2" > "$_f"
  aws ssm put-parameter --region "$region" --name "$1" --type SecureString --value "file://$_f" --overwrite >/dev/null
  rm -f "$_f"
}
stack_name="${STACK_NAME:-todoist-apple-calendar-sync}"
namespace="${PARAMETER_NAMESPACE:-/todoist-apple-calendar}"
deployment_name="${DEPLOYMENT_NAME:-todoist-apple-calendar-sync}"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [options]

Options:
  --region REGION          AWS region (default: AWS_REGION or ap-south-1)
  --stack-name NAME        CloudFormation stack name
  --name NAME              Lambda, IAM role, and EventBridge name prefix
  --namespace PATH         SSM Parameter Store path prefix
  --calendar NAME          Existing iCloud calendar name
  --timezone IANA_NAME     Timezone for Todoist floating times
  --alarm-minutes MINUTES  Alert lead time for timed tasks
  --no-icloud              Skip the iCloud sink (Google only)
  --google-calendar ID     Dedicated Google calendar shared with the service account
  --google-sa-file PATH    Service-account key JSON (stored in SSM as SecureString; never echoed)
  --google-busy IDS        Comma-separated Google calendar IDs for free/busy in the week digest
  --week-digest            Create the bearer-protected week digest URL (secret generated and stored in SSM)
  -h, --help               Show this help text

Secrets are always collected through hidden prompts.
EOF
}

calendar_name="${APPLE_CALENDAR_NAME:-}"
display_timezone="${DISPLAY_TIMEZONE:-Asia/Kolkata}"
alarm_minutes="${ALARM_MINUTES_BEFORE:-10}"
icloud=1 google_calendar="" google_sa_file="" google_busy="" week_digest=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) region="$2"; shift 2 ;;
    --stack-name) stack_name="$2"; shift 2 ;;
    --name) deployment_name="$2"; shift 2 ;;
    --namespace) namespace="$2"; shift 2 ;;
    --calendar) calendar_name="$2"; shift 2 ;;
    --timezone) display_timezone="$2"; shift 2 ;;
    --alarm-minutes) alarm_minutes="$2"; shift 2 ;;
    --no-icloud) icloud=0; shift ;;
    --google-calendar) google_calendar="$2"; shift 2 ;;
    --google-sa-file) google_sa_file="$2"; shift 2 ;;
    --google-busy) google_busy="$2"; shift 2 ;;
    --week-digest) week_digest=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

account_id="$(aws sts get-caller-identity --region "$region" --query Account --output text)"
artifact_bucket="todoist-apple-calendar-artifacts-${account_id}-${region}"

apple_email="" apple_password=""
if [[ "$icloud" == 1 ]]; then
  read -r -p "Apple Account email: " apple_email
  if [[ -z "$calendar_name" ]]; then
    read -r -p "iCloud calendar name [Home]: " calendar_name
    calendar_name="${calendar_name:-Home}"
  fi
fi
read -r -s -p "Todoist API token (input hidden): " todoist_token
printf '\n'
if [[ "$icloud" == 1 ]]; then
  read -r -s -p "Apple app-specific password (input hidden): " apple_password
  printf '\n'
  if [[ -z "$apple_email" || -z "$apple_password" ]]; then
    echo "Apple email and app-specific password are required (or pass --no-icloud)." >&2
    exit 2
  fi
fi
if [[ -z "$todoist_token" ]]; then echo "The Todoist token is required." >&2; exit 2; fi
if [[ -n "$google_calendar" && ! -f "$google_sa_file" ]]; then
  echo "--google-calendar needs --google-sa-file with the service-account key JSON." >&2
  exit 2
fi
if [[ "$icloud" == 0 && -z "$google_calendar" ]]; then echo "Nothing to sync: enable iCloud or pass --google-calendar." >&2; exit 2; fi

"$root_dir/scripts/build.sh"
artifact_key="lambda/$(shasum -a 256 "$root_dir/.build/lambda.zip" | awk '{print $1}').zip"

if ! aws s3api head-bucket --bucket "$artifact_bucket" --region "$region" 2>/dev/null; then
  aws s3api create-bucket --bucket "$artifact_bucket" --region "$region" \
    --create-bucket-configuration "LocationConstraint=$region" >/dev/null
  aws s3api put-public-access-block --bucket "$artifact_bucket" --region "$region" \
    --public-access-block-configuration 'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
fi

aws s3 cp "$root_dir/.build/lambda.zip" "s3://$artifact_bucket/$artifact_key" --region "$region" >/dev/null
aws cloudformation deploy \
  --region "$region" \
  --stack-name "$stack_name" \
  --template-file "$root_dir/infra/template.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "Namespace=$namespace" "DeploymentName=$deployment_name" "AppleAccountEmail=$apple_email" "AppleCalendarName=$calendar_name" "DisplayTimezone=$display_timezone" "AlarmMinutesBefore=$alarm_minutes" "ArtifactBucket=$artifact_bucket" "ArtifactKey=$artifact_key" \
    "GoogleCalendarId=$google_calendar" "GoogleBusyCalendarIds=$google_busy" "EnableWeekDigest=$week_digest"

put_secret "$namespace/todoist-token" "$todoist_token"
if [[ "$icloud" == 1 ]]; then
  put_secret "$namespace/apple-app-password" "$apple_password"
fi
if [[ -n "$google_calendar" ]]; then
  aws ssm put-parameter --region "$region" --name "$namespace/google-service-account" --type SecureString --value "file://$google_sa_file" --overwrite >/dev/null
fi
if [[ "$week_digest" == true ]]; then
  if ! aws ssm get-parameter --region "$region" --name "$namespace/digest-token" >/dev/null 2>&1; then
    aws ssm put-parameter --region "$region" --name "$namespace/digest-token" --type SecureString --value "file://$(t="$(mktemp)"; chmod 600 "$t"; openssl rand -hex 32 | tr -d "\n" > "$t"; echo "$t")" >/dev/null
  fi
  digest_url="$(aws cloudformation describe-stacks --region "$region" --stack-name "$stack_name" --query "Stacks[0].Outputs[?OutputKey=='DigestUrl'].OutputValue" --output text)"
  echo "Week digest URL: $digest_url"
  echo "Read the bearer secret once with: aws ssm get-parameter --region $region --name $namespace/digest-token --with-decryption --query Parameter.Value --output text"
fi

aws lambda invoke --region "$region" --function-name "$deployment_name" \
  --invocation-type RequestResponse "$root_dir/.build/invoke.json" >/dev/null
cat "$root_dir/.build/invoke.json"
