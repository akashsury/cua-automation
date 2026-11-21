#!/usr/bin/env bash
# End-to-end demo. Produces evidence/runs/* and evidence/demo/*.json.
#   discovery (real LLM) -> artifacts -> deterministic replays incl. errors and a human takeover.
# Requires ANTHROPIC_API_KEY (in .env). Set SKIP_DISCOVERY=1 to only replay existing artifacts.
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
set -a; [ -f .env ] && source .env; set +a
OUT=evidence/demo; mkdir -p "$OUT"
URL=http://127.0.0.1:8765

start_app() {  # $1 = tenant
  pkill -f "cua mockbank" >/dev/null 2>&1; sleep 1
  cua mockbank --tenant "$1" > /dev/null 2>&1 &
  for _ in $(seq 50); do curl -s -o /dev/null "$URL/login" && break; sleep 0.2; done
  curl -s -X POST "$URL/__reset" > /dev/null
}
run() {  # $1 = name, rest = command
  local name=$1; shift
  echo -e "\n=== $name: cua $*"
  cua "$@" > "$OUT/$name.json"; local rc=$?
  echo "exit=$rc"; head -c 900 "$OUT/$name.json"; echo
}

start_app a
if [ -z "${SKIP_DISCOVERY:-}" ]; then
  run 01-discover-sign-on discover --id mockcore.sign_on --entry /login \
      --goal "Sign on to the core system with the service account and reach the Member Inquiry screen"
  cua approve mockcore.sign_on
  run 02-discover-lookup discover --id mockcore.lookup_member_balance --requires mockcore.sign_on \
      --param member_id=12345 --outputs "savings_balance (decimal)" \
      --goal "Look up member 12345 and read their current savings balance"
  run 03-discover-open-share discover --id mockcore.open_share --requires mockcore.sign_on \
      --param member_id=23456 --param share_type=SAVINGS --param nickname=VACATION --param deposit=25.00 \
      --outputs "confirmation_no (string)" --operator scripted:examples/operator/approve.json \
      --goal "Open a new SAVINGS share nicknamed VACATION with an initial deposit of 25.00 for member 23456, reach the confirmation screen and return the confirmation number"
  cua approve mockcore.lookup_member_balance
fi
start_app a   # fresh core data for replays (discovery 03 really opened a share for member 23456)

L=mockcore.lookup_member_balance
run 10-replay-success          replay $L --param member_id=12345
run 11-replay-other-member     replay $L --param member_id=23456
run 12-replay-not-found        replay $L --param member_id=99999
run 13-replay-invalid-input    replay $L --param member_id=12AB
run 14-replay-interstitial     replay $L --param member_id=12345 --inject-fault interstitial
run 15-replay-session-expired  replay $L --param member_id=12345 --inject-fault session_timeout
run 16-replay-slow             replay $L --param member_id=12345 --inject-fault slow
run 17-replay-server-error     replay $L --param member_id=12345 --inject-fault server_error
run 18-replay-unknown-no-operator replay $L --param member_id=12345 --inject-fault supervisor_prompt
run 19-replay-human-takeover   replay $L --param member_id=12345 --inject-fault supervisor_prompt \
                                 --operator scripted:examples/operator/acknowledge.json
O=mockcore.open_share
P="--param member_id=12345 --param share_type=SAVINGS --param nickname=TRAVEL --param deposit=40.00"
run 20-replay-open-share-no-approval replay $O $P
run 21-replay-open-share-approved    replay $O $P --approve-irreversible
run 22-replay-open-share-denied-perm replay $O $P --approve-irreversible --inject-fault permission_denied
# 21 gave member 12345 a second SAVINGS share, so "the savings balance" is now ambiguous:
# replay must refuse to guess (locator_ambiguous) rather than return the wrong row.
run 23-replay-ambiguous-after-change replay $L --param member_id=12345

# Tenant B: same vendor product, different labels/branding.
start_app b
run 30-tenant-b-base-artifact  replay $L --param member_id=12345
run 31-tenant-b-with-override  replay $L --param member_id=12345 --tenant b
pkill -f "cua mockbank" >/dev/null 2>&1
echo -e "\nDone. Results in $OUT, run evidence in evidence/runs/"
