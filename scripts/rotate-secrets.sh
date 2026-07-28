#!/usr/bin/env bash
set -euo pipefail

region="${AWS_REGION:-ap-south-1}"
namespace="/todoist-apple-calendar"

read -r -s -p "Todoist API token (input hidden): " todoist_token
printf '\n'
read -r -s -p "Apple app-specific password (input hidden): " apple_password
printf '\n'

if [[ -z "$todoist_token" || -z "$apple_password" ]]; then
  echo "Both secrets are required." >&2
  exit 2
fi

aws ssm put-parameter --region "$region" --name "$namespace/todoist-token" \
  --type SecureString --value "$todoist_token" --overwrite >/dev/null
aws ssm put-parameter --region "$region" --name "$namespace/apple-app-password" \
  --type SecureString --value "$apple_password" --overwrite >/dev/null
