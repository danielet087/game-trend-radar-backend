#!/usr/bin/env bash
# Authenticate a single frontend git command without putting the PAT in a URL,
# command-line argument or persistent Git configuration.
set -euo pipefail
if [[ -z "${FRONTEND_REPO_TOKEN:-}" ]]; then
  echo "FRONTEND_REPO_TOKEN is not configured" >&2
  exit 1
fi
askpass="$(mktemp "${RUNNER_TEMP:-/tmp}/gtr-askpass.XXXXXXXX")"
trap 'rm -f "$askpass"' EXIT
cat > "$askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "$FRONTEND_REPO_TOKEN" ;;
  *) exit 1 ;;
esac
ASKPASS
chmod 700 "$askpass"
GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git -c credential.helper= "$@"
