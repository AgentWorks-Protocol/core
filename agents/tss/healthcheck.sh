#!/usr/bin/env bash
# Lightweight uptime check for the AgentWorks TSS signer (run by cron every 5 min on the signer host).
# Self-heals a stopped or wedged container and records a status line; optional webhook alert on non-OK.
#
# Health signal (most reliable, host-side — no in-container tools needed):
#   - container running?                              docker inspect .State.Running
#   - any profile permanently gave up?               entrypoint prints "FATAL:" after MAX_RETRIES
#   - how many signer processes are alive right now?  docker top | grep "cobo-tss-node start"  (expect 5)
#
# Optional alerting: put  HEALTH_WEBHOOK=https://…  in /root/signer/health.env (Slack/Discord/ntfy.sh).
# Log:  /var/log/signer-health.log  (bounded to the last 2000 lines).
set -uo pipefail

NAME=agentworks-signer
DIR=/root/signer
FILE=docker-compose.prod.yml
EXPECT=5
LOG=/var/log/signer-health.log
[ -f "$DIR/health.env" ] && . "$DIR/health.env"

ts() { date -u +%FT%TZ; }
record() {   # $1=status $2=message
  echo "$(ts) [$1] $2" >> "$LOG"
  if [ -n "${HEALTH_WEBHOOK:-}" ] && [ "$1" != "OK" ]; then
    curl -fsS -m 10 -H 'Content-Type: application/json' \
      -d "{\"text\":\"[$NAME][$1] $2\"}" "$HEALTH_WEBHOOK" >/dev/null 2>&1 || true
  fi
}
heal()    { ( cd "$DIR" && docker compose -f "$FILE" up -d ) >/dev/null 2>&1; }
restart() { ( cd "$DIR" && docker compose -f "$FILE" restart ) >/dev/null 2>&1; }

running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || echo missing)
if [ "$running" != "true" ]; then
  heal
  record ALERT "container was not running ($running) — brought back up"
elif docker logs --since 15m "$NAME" 2>&1 | grep -q "FATAL:"; then
  restart
  record ALERT "a signer profile hit FATAL (gave up after retries) — restarted container"
else
  alive=$(docker top "$NAME" -o pid,cmd 2>/dev/null | grep -c "cobo-tss-node start")
  if   [ "$alive" -ge "$EXPECT" ]; then record OK   "$alive/$EXPECT signer processes up"
  elif [ "$alive" -eq 0 ];        then restart; record ALERT "0/$EXPECT signer processes — restarted container"
  else                                 record WARN "$alive/$EXPECT signer processes (one in retry/backoff; recovering)"
  fi
fi

tail -n 2000 "$LOG" 2>/dev/null > "$LOG.tmp" && mv "$LOG.tmp" "$LOG" || true
