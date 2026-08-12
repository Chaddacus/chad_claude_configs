#!/usr/bin/env bash
# Fetch Lead B (Matt) status from his tailnet-served endpoint.
#
# Prints a machine-readable STATE line first, then the payload (if any).
# States are distinguished on purpose — "no answer" has several very
# different causes and conflating them produces wrong conclusions about
# a teammate's lane:
#
#   OK              service answered 2xx; payload follows
#   HTTP_<code>     service answered but not 2xx (auth? wrong path?)
#   NO_SERVICE      host is UP on the tailnet, nothing listening/serving
#   HOST_DOWN       host not reachable on the tailnet at all
#   NO_TAILSCALE    tailscale CLI unavailable on this machine
#
# Exit code is 0 for OK, 1 otherwise, so callers can branch.
set -uo pipefail

URL="${1:-https://matthews-macbook-pro.tailcc6c5f.ts.net/zw-migration?format=text}"
HOST=$(printf '%s' "$URL" | sed -E 's#^[a-z]+://##; s#[:/].*$##')
TIMEOUT="${ZW_STATUS_TIMEOUT:-10}"
TS_BIN="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"

emit() { echo "STATE=$1"; }

# 1. Try the endpoint first — the happy path costs one request.
BODY_FILE=$(mktemp)
# NB: on a connection failure curl ALREADY prints 000 here and exits
# non-zero — do not add a `|| echo 000` fallback, it concatenates into
# "000000" and the guard below stops matching (caught in testing).
CODE=$(curl -sk -o "$BODY_FILE" -w '%{http_code}' --max-time "$TIMEOUT" "$URL" 2>/dev/null)
[ -z "$CODE" ] && CODE=000

if [ "$CODE" != "000" ]; then
    if [ "$CODE" -ge 200 ] && [ "$CODE" -lt 300 ]; then
        emit OK
        echo "URL=$URL"
        echo "FETCHED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "--- payload ---"
        cat "$BODY_FILE"
        rm -f "$BODY_FILE"
        exit 0
    fi
    emit "HTTP_$CODE"
    echo "URL=$URL"
    # 401/403 from this endpoint is an IDENTITY problem, not a service problem.
    # Tailscale injects the caller's user identity into the proxied request;
    # a TAGGED node has no user identity to inject, so it gets 401 even though
    # the service is healthy. Verified 2026-07-29 in both directions: a tagged
    # node gets 401 from Matt's board AND from Chad's, so it is symmetric and
    # not a misconfiguration on either side. Do NOT read it as "lane is down".
    if [ "$CODE" = "401" ] || [ "$CODE" = "403" ]; then
        echo "CAUSE=no user identity on this node (tagged device?)"
        echo "FIX=run this from a user-owned node; verify with:"
        echo "    tailscale status --json | grep -E '\"Tags\"|LoginName'"
        echo "NOTE=the service is UP -- this says nothing about the lane."
    fi
    echo "--- body (first 500 chars) ---"
    head -c 500 "$BODY_FILE"
    rm -f "$BODY_FILE"
    exit 1
fi
rm -f "$BODY_FILE"

# 2. No HTTP answer. Separate "host down" from "host up, not serving" —
#    they mean completely different things for a teammate's lane.
if [ ! -x "$TS_BIN" ]; then
    emit NO_TAILSCALE
    echo "Could not distinguish host-down from not-serving: no tailscale CLI."
    exit 1
fi

if timeout 12 "$TS_BIN" ping --c 2 "$HOST" >/dev/null 2>&1; then
    emit NO_SERVICE
    echo "HOST=$HOST is UP on the tailnet, but nothing answered HTTP at $URL."
    echo "Most likely: the endpoint is not being served right now"
    echo "(e.g. 'tailscale serve' not running on that machine)."
    echo "This is EXPECTED when Lead B's machine is on but the status"
    echo "surface is not up — it is NOT evidence about his lane's state."
else
    emit HOST_DOWN
    echo "HOST=$HOST is not reachable on the tailnet (asleep/offline/ACL)."
    # "HOST_DOWN" on its own invites the wrong follow-up ("is my serve
    # config broken? wrong port? cert?"). The control plane already knows,
    # so quote it. LastHandshake is the decisive field: a zero value
    # (0001-01-01) means no WireGuard session was EVER established with
    # that peer this session -- it did not drop mid-session, it never came
    # up. RxBytes=0 against a nonzero TxBytes says the same thing from the
    # traffic side. Neither is affected by anything on OUR end.
    "$TS_BIN" status --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
host = sys.argv[1].split('.')[0]
for p in (d.get('Peer') or {}).values():
    if host in ((p.get('HostName') or '') + (p.get('DNSName') or '')):
        hs = p.get('LastHandshake') or ''
        print('--- control plane ---')
        print('  Online:        %s' % p.get('Online'))
        print('  LastSeen:      %s' % p.get('LastSeen'))
        print('  LastHandshake: %s%s' % (
            hs, '   <- zero value: no session EVER established' if hs.startswith('0001-01-01') else ''))
        print('  CurAddr:       %r' % (p.get('CurAddr') or ''))
        print('  Tx/Rx bytes:   %s / %s%s' % (
            p.get('TxBytes'), p.get('RxBytes'),
            '   <- we sent, nothing came back' if not p.get('RxBytes') else ''))
        break
" "$HOST"
    echo "VERDICT: his machine is off/asleep. Nothing to fix on our side."
    echo "Fall back to the REPO (docs/consolidation/CURRENT-STATE.md on"
    echo "origin/development) -- that is the durable channel. Both boards are"
    echo "laptop-served, so neither is reliable state; check origin for his"
    echo "pushes instead: git log HEAD..origin/development --oneline"
fi
exit 1
