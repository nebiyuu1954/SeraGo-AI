#!/usr/bin/env bash
# =============================================================================
# SeraGo-AI keep-warm loop
# =============================================================================
# Why this exists
#   The AI service's read path (`scores/batch`, which annotates the "For You"
#   feed) is a plain DB read, but its Neon database autosuspends after ~5
#   minutes idle. The first read after a suspend pays a wake-up penalty:
#   measured ~1.5-2.3s warm vs 17.1s cold. That cold read is what made the
#   feed silently fall back to null scores.
#
#   Pinging /api/matching/health keeps BOTH sides warm:
#     - it runs four COUNT(*) queries against Neon, so the compute never
#       idles long enough to suspend;
#     - it keeps the WSGI worker hot.
#
#   The endpoint is deliberately public (listed in _PUBLIC_PATHS), so this
#   script needs no API key.
#
# Usage
#   ./scripts/keep-warm.sh                       # localhost:8001, every 240s
#   ./scripts/keep-warm.sh 300                   # custom interval (seconds)
#   KEEPWARM_URL=https://ai.example.com ./scripts/keep-warm.sh
#
#   Run it alongside cloudflared, in its own terminal or as a systemd unit.
#
# Note on timing
#   The interval is measured from the START of one cycle to the start of the
#   next, so slow pings do not push the cadence out. (Naive `curl; sleep N`
#   loops drift: on Windows/Git Bash a 1s sleep takes ~1.2s and each
#   subprocess spawn ~250ms, which nearly doubles the effective interval and
#   can let the database suspend anyway.)
# =============================================================================

set -u

URL="${KEEPWARM_URL:-http://127.0.0.1:8001}"
INTERVAL="${1:-${KEEPWARM_INTERVAL:-240}}"
SLOW_MS="${KEEPWARM_SLOW_MS:-5000}"

HEALTH_PATH="/api/matching/health"
ENDPOINT="${URL%/}${HEALTH_PATH}"

case "$INTERVAL" in
  ''|*[!0-9]*) echo "keep-warm: interval must be an integer number of seconds (got '$INTERVAL')" >&2; exit 2 ;;
esac
case "$SLOW_MS" in
  ''|*[!0-9]*) echo "keep-warm: KEEPWARM_SLOW_MS must be an integer (got '$SLOW_MS')" >&2; exit 2 ;;
esac

if [ "$INTERVAL" -ge 300 ]; then
  echo "keep-warm: WARNING — interval ${INTERVAL}s is >= Neon's ~5 minute autosuspend" >&2
  echo "keep-warm: window, so the database can suspend between pings and you will" >&2
  echo "keep-warm: still hit cold reads. 240s or lower is recommended." >&2
fi

pings=0
failures=0
slow=0
running=1

on_stop() {
  running=0
  printf '\nkeep-warm: stopping — pings=%s failures=%s slow(%sms+)=%s\n' \
    "$pings" "$failures" "$SLOW_MS" "$slow"
}
trap on_stop INT TERM

echo "keep-warm: ${ENDPOINT} every ${INTERVAL}s (slow threshold ${SLOW_MS}ms) — Ctrl-C to stop"

while [ "$running" -eq 1 ]; do
  cycle_start=$SECONDS
  ts="$(date '+%Y-%m-%d %H:%M:%S')"

  # Single curl; the last line carries status + duration so no parsing helpers
  # (awk/sed/tail/cut) are spawned per cycle.
  resp="$(curl -s -m 30 -o - -w $'\n%{http_code} %{time_total}' "$ENDPOINT" 2>/dev/null)"
  rc=$?

  if [ "$running" -eq 0 ]; then
    break
  fi

  if [ "$rc" -ne 0 ]; then
    failures=$((failures + 1))
    printf '%s  FAIL   curl exit %s (service/tunnel down — will retry)\n' "$ts" "$rc"
  else
    meta="${resp##*$'\n'}"
    json="${resp%$'\n'*}"
    code="${meta%% *}"
    secs="${meta##* }"

    # seconds -> milliseconds without spawning awk.
    whole="${secs%%.*}"
    frac="${secs#*.}"
    [ "$frac" = "$secs" ] && frac="0"
    frac3="${frac:0:3}"
    ms=$(( ${whole:-0} * 1000 + 10#${frac3:-0} ))

    if [ "$code" != "200" ]; then
      failures=$((failures + 1))
      printf '%s  HTTP %s  %sms  %s\n' "$ts" "$code" "$ms" "${json:0:110}"
    else
      pings=$((pings + 1))
      if [ "$ms" -ge "$SLOW_MS" ]; then
        slow=$((slow + 1))
        printf '%s  OK %sms (SLOW — likely a cold read)  %s\n' "$ts" "$ms" "${json:0:110}"
      else
        printf '%s  OK %sms  %s\n' "$ts" "$ms" "${json:0:110}"
      fi
    fi
  fi

  # Sleep out the remainder of the interval, in 1s slices so Ctrl-C is
  # responsive. Uses the SECONDS builtin — no process spawn.
  while [ "$running" -eq 1 ] && [ $((SECONDS - cycle_start)) -lt "$INTERVAL" ]; do
    sleep 1
  done
done
