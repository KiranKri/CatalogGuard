#!/usr/bin/env bash
# Boots Compose and checks the core walkthrough. Does not claim a green run
# until that run has been pasted back.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

cleanup() {
  docker compose down -v || true
}
trap cleanup EXIT

docker compose up -d --build

ok=0
for _ in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null; then
    ok=1
    break
  fi
  sleep 2
done
test "$ok" = 1

B=http://127.0.0.1:8000
body=$(curl -sf -X PUT "$B/api/v1/inventory/BKPK-28L-OLV/nyc" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: compose-bkpk-nyc-1" \
  --data-raw '{"quantity":2,"expected_version":1}')
python3 -c 'import json,sys; assert json.loads(sys.argv[1])["version"] == 2' "$body"

python3 -c 'import json,sys,urllib.request
base=sys.argv[1]
def count(path):
    with urllib.request.urlopen(base+path) as response:
        return len(json.load(response)["items"])
assert count("/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc") == 1
assert count("/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc") == 1
' "$B"

replay=$(curl -s -D - -o /dev/null -X PUT "$B/api/v1/inventory/BKPK-28L-OLV/nyc" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: compose-bkpk-nyc-1" \
  --data-raw '{"quantity":2,"expected_version":1}')
echo "$replay" | grep -qi 'idempotency-replayed: true'

mailer=$(docker compose exec -T api python -m catalogguard.mailer)
echo "$mailer" | grep -q '"handed_off": 1'
