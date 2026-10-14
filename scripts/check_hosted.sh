#!/usr/bin/env bash
# Smoke-check the hosted demo (D9) from outside: public routes answer, private ones refuse, and
# time to first byte (a cold start shows up here). Usage:
#
#   scripts/check_hosted.sh https://quietcheck-backend.onrender.com https://quietcheck-web.onrender.com
#
# Needs curl and python3. The checks above the deep one never send a token or password, so they
# check what a stranger sees. With BACKEND_API_TOKEN in the environment it also calls
# GET /health/deep and fails unless it reports ok (database, members, lists, workflow; D10). The
# hourly GitHub Action (.github/workflows/hosted-check.yml) runs it that way.
set -uo pipefail

BACKEND="${1:?backend URL}"; WEB="${2:?web URL}"
BACKEND="${BACKEND%/}"; WEB="${WEB%/}"
fail=0

check() { # check "what" expected_status url [curl args...]
  local what="$1" want="$2" url="$3"; shift 3
  local out code secs
  out=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 90 "$@" "$url") || true
  code=${out%% *}; secs=${out##* }
  if [[ "$code" == "$want" ]]; then
    printf '  ok    %-44s %s in %ss\n' "$what" "$code" "$secs"
  else
    printf '  FAIL  %-44s got %s, want %s (%ss)\n' "$what" "$code" "$want" "$secs"; fail=1
  fi
}

echo "Backend $BACKEND"
check "GET /health (public)" 200 "$BACKEND/health"
check "GET order status, unknown order (public)" 404 "$BACKEND/merchant/orders/ord_none/status"
check "GET /catalog without token (private)" 401 "$BACKEND/catalog"
check "GET /merchant/orders without token (private)" 401 "$BACKEND/merchant/orders"
check "POST /payments/orders without token (private)" 401 "$BACKEND/payments/orders" \
  -X POST -H 'Content-Type: application/json' -d '{}'
check "POST /webhooks/paypal unsigned (rejected)" 400 "$BACKEND/webhooks/paypal" \
  -X POST -H 'Content-Type: application/json' -d '{"id":"x","event_type":"X"}'

echo "Web $WEB"
check "GET / (shop, public)" 200 "$WEB/"
check "GET /console without password" 401 "$WEB/console"
check "GET /api/console/orders without password" 401 "$WEB/api/console/orders"
check "GET /api/orders/{id}/status (public)" 404 "$WEB/api/orders/ord_none/status"

if [[ -n "${BACKEND_API_TOKEN:-}" ]]; then
  echo "Deep health"
  # The header goes through a file descriptor, so the token never appears in the process list.
  body=$(curl -s --max-time 60 -H @<(printf 'Authorization: Bearer %s\n' "$BACKEND_API_TOKEN") \
    "$BACKEND/health/deep") || body=""
  # One line per part; exits 1 unless the report says ok. The report holds no secrets.
  python3 - "$body" <<'PY' || fail=1
import json
import sys

try:
    r = json.loads(sys.argv[1])
    r["ok"], r["database"], r["members"], r["workflow"]
except (ValueError, KeyError, TypeError):
    print("  FAIL  /health/deep gave no report (wrong token, or the backend is down)")
    sys.exit(1)


def line(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}")


line(r["database"]["ok"], "database")
for m in r["members"]:
    problem = m.get("error") or ("URL mismatch" if not m.get("url_matches") else "")
    if not problem and m.get("list_matches_expected") is False:
        problem = "list differs from MEMBER_DATA_HASHES"
    line(m["ok"] and not problem, f"member {m['member']} {problem}".rstrip())
line(r["workflow"]["configured"], f"workflow {r['workflow'].get('task')}")
sys.exit(0 if r["ok"] else 1)
PY
fi

exit $fail
