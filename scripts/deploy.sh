#!/usr/bin/env bash
set -euo pipefail

region="${AWS_REGION:-ap-south-1}"
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
  -h, --help               Show this help text

Secrets are always collected through hidden prompts.
EOF
}

calendar_name="${APPLE_CALENDAR_NAME:-}"
display_timezone="${DISPLAY_TIMEZONE:-Asia/Kolkata}"
alarm_minutes="${ALARM_MINUTES_BEFORE:-10}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) region="$2"; shift 2 ;;
    --stack-name) stack_name="$2"; shift 2 ;;
    --name) deployment_name="$2"; shift 2 ;;
    --namespace) namespace="$2"; shift 2 ;;
    --calendar) calendar_name="$2"; shift 2 ;;
    --timezone) display_timezone="$2"; shift 2 ;;
    --alarm-minutes) alarm_minutes="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

account_id="$(aws sts get-caller-identity --region "$region" --query Account --output text)"
artifact_bucket="todoist-apple-calendar-artifacts-${account_id}-${region}"

read -r -p "Apple Account email: " apple_email
if [[ -z "$calendar_name" ]]; then
  read -r -p "iCloud calendar name [Home]: " calendar_name
  calendar_name="${calendar_name:-Home}"
fi
read -r -s -p "Todoist API token (input hidden): " todoist_token
printf '\n'
read -r -s -p "Apple app-specific password (input hidden): " apple_password
printf '\n'

if [[ -z "$apple_email" || -z "$todoist_token" || -z "$apple_password" ]]; then
  echo "Apple email and both secrets are required." >&2
  exit 2
fi

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
  --parameter-overrides "Namespace=$namespace" "DeploymentName=$deployment_name" "AppleAccountEmail=$apple_email" "AppleCalendarName=$calendar_name" "DisplayTimezone=$display_timezone" "AlarmMinutesBefore=$alarm_minutes" "ArtifactBucket=$artifact_bucket" "ArtifactKey=$artifact_key"

aws ssm put-parameter --region "$region" --name "$namespace/todoist-token" --type SecureString --value "$todoist_token" --overwrite >/dev/null
aws ssm put-parameter --region "$region" --name "$namespace/apple-app-password" --type SecureString --value "$apple_password" --overwrite >/dev/null

aws lambda invoke --region "$region" --function-name "$deployment_name" \
  --invocation-type RequestResponse "$root_dir/.build/invoke.json" >/dev/null
cat "$root_dir/.build/invoke.json"
