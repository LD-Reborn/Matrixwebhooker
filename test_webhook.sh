#!/usr/bin/env bash
# Quick end-to-end test for the local webhook server.
set -euo pipefail
cd "$(dirname "$0")"

BASE="${BASE:-http://127.0.0.1:8080}"
# shellcheck disable=SC1091
source .env
: "${API_KEY:?API_KEY not set in .env}"

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== health =="
curl -fsS "$BASE/health"
echo

echo "== authorized send (m.text) =="
curl -fsS -X POST "$BASE/webhook" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"Webhooker test alert at $STAMP (m.text)\"}"
echo

echo "== authorized send (m.notice + html) =="
curl -fsS -X POST "$BASE/webhook" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"notice test $STAMP\", \"msgtype\": \"m.notice\", \"html\": \"<b>bold</b> and <code>code</code> ($STAMP)\"}"
echo

echo "== unauthenticated send must fail with 401 =="
set +e
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/webhook" \
  -H "Content-Type: application/json" -d '{"text": "should not pass"}')
set -e
echo "got HTTP $CODE (expected 401)"
[ "$CODE" = "401" ] || { echo "FAILED: expected 401"; exit 1; }
echo "ALL OK"
