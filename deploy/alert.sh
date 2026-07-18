#!/usr/bin/env bash
# CORTANA failure alert hook — run by cortana-alert@.service with the failing
# unit's name as $1. Installed to /opt/cortana/alert.sh by deploy/install.sh
# (fixed path on purpose: it must survive a broken /opt/cortana/current).
#
# Behaviour:
#   1. Always append one line to /var/lib/cortana/alerts.log.
#   2. If /etc/cortana/alert_webhook exists and is non-empty (mode 0600,
#      root-owned — the URL is a secret and never appears in env/YAML/repo,
#      constraint 12), POST the failing unit's last 15 journal lines to it.
#   3. NEVER fail. Alerting runs exactly when things are broken; a missing
#      webhook, dead network, or unreadable journal degrades, never aborts.
#
# Deliberately no `set -e`: every step is individually best-effort.
set -u

UNIT="${1:-unknown-unit}"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG=/var/lib/cortana/alerts.log
WEBHOOK_FILE=/etc/cortana/alert_webhook

mkdir -p /var/lib/cortana 2>/dev/null || true
echo "${STAMP} ${UNIT} entered failed state" >> "${LOG}" 2>/dev/null || true

# --- webhook (optional) ----------------------------------------------------
[[ -s "${WEBHOOK_FILE}" ]] || exit 0
WEBHOOK_URL="$(head -n1 "${WEBHOOK_FILE}" 2>/dev/null | tr -d '[:space:]')"
[[ -n "${WEBHOOK_URL}" ]] || exit 0

TAIL="$(journalctl -u "${UNIT}" -n 15 --no-pager -o short-iso 2>/dev/null)"
[[ -n "${TAIL}" ]] || TAIL="(journal unavailable)"

# Discord caps message content at 2000 chars — keep the newest journal lines
# and let python do the JSON escaping (python3 is a system package here).
# shellcheck disable=SC2016  # the single-quoted $-free python is intentional
PAYLOAD="$(UNIT="${UNIT}" STAMP="${STAMP}" TAIL="${TAIL}" python3 -c '
import json, os
tail = os.environ["TAIL"][-1500:]
content = "ALERT: {} failed at {}\n```\n{}\n```".format(
    os.environ["UNIT"], os.environ["STAMP"], tail)
print(json.dumps({"content": content}))
' 2>/dev/null)"
[[ -n "${PAYLOAD}" ]] || exit 0

curl -fsS -m 10 -H 'Content-Type: application/json' \
    -d "${PAYLOAD}" "${WEBHOOK_URL}" >/dev/null 2>&1 \
    || echo "${STAMP} ${UNIT}: webhook post failed" >> "${LOG}" 2>/dev/null

exit 0
