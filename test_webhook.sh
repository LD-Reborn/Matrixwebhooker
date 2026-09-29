#!/usr/bin/env bash
# Quick end-to-end test for the webhook server (all bots in bots.json).
set -euo pipefail
cd "$(dirname "$0")"

BASE="${BASE:-http://127.0.0.1:8080}"

bot_field() {  # $1 = bot name, $2 = field
  python3 - "$1" "$2" <<'EOF'
import json, sys
bots = {b["name"]: b for b in json.load(open("bots.json"))["bots"]}
print(bots[sys.argv[1]][sys.argv[2]])
EOF
}

BOT_NAMES=$(python3 -c "import json; print(' '.join(b['name'] for b in json.load(open('bots.json'))['bots']))")
for BOT in $BOT_NAMES; do
  KEY=$(bot_field "$BOT" api_key)

  echo "== bot: $BOT =="
  echo "-- me --"
  curl -fsS "$BASE/me" -H "X-API-Key: $KEY"; echo
  echo "-- send (m.text) --"
  curl -fsS -X POST "$BASE/webhook" \
    -H "X-API-Key: $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"Webhooker test: $BOT at $(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
  echo
done

echo "== health =="
curl -fsS "$BASE/health"; echo

echo "== unauthenticated send must fail with 401 =="
set +e
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/webhook" \
  -H "Content-Type: application/json" -d '{"text": "should not pass"}')
set -e
echo "got HTTP $CODE (expected 401)"
[ "$CODE" = "401" ] || { echo "FAILED: expected 401"; exit 1; }
echo "ALL OK"
