#!/usr/bin/env bash
# Smoke-check the hosted demo (D9) from outside: public routes answer, private ones refuse, and
# time to first byte (a cold start shows up here). Usage:
#
#   scripts/check_hosted.sh https://quietcheck-backend.onrender.com https://quietcheck-web.onrender.com
#
# Needs only curl. It never sends a token or password, so it checks what a stranger sees.
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

exit $fail
