#!/usr/bin/env bash
set -euo pipefail

region="${AWS_REGION:-ap-south-1}"
# Secrets go to SSM from a 0600 temp file (file://), never as a command-line argument visible in `ps`.
put_secret() {
  _f="$(mktemp)"; chmod 600 "$_f"; printf '%s' "$2" > "$_f"
  aws ssm put-parameter --region "$region" --name "$1" --type SecureString --value "file://$_f" --overwrite >/dev/null
  rm -f "$_f"
}
namespace="/todoist-apple-calendar"

read -r -s -p "Todoist API token (input hidden): " todoist_token
printf '\n'
read -r -s -p "Apple app-specific password (input hidden): " apple_password
printf '\n'

if [[ -z "$todoist_token" || -z "$apple_password" ]]; then
  echo "Both secrets are required." >&2
  exit 2
fi

put_secret "$namespace/todoist-token" "$todoist_token"
put_secret "$namespace/apple-app-password" "$apple_password"
