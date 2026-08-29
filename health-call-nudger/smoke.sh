#!/usr/bin/env bash
# Local smoke test: boots the service on a scratch dir and exercises every endpoint.
set -u
PORT=4399
DIR=$(mktemp -d)
cp health_call_nudger.js api.json package.json "$DIR/"
ln -s "$PWD/node_modules" "$DIR/node_modules"
printf '{"stressThreshold":75,"cooldownSeconds":1800,"listenerPort":%s}\n' "$PORT" > "$DIR/config.json"

echo "=== boot with NO env (expect warning, service still up) ==="
(cd "$DIR" && env -u ELEVENLABS_API_KEY node health_call_nudger.js > boot.log 2>&1 &)
sleep 1
cat "$DIR/boot.log"
B="http://127.0.0.1:$PORT"

echo "=== GET / ===";        curl -s "$B/" | head -5
echo "=== GET /status ===";  curl -s "$B/status"
echo "=== GET /config ===";  curl -s "$B/config"
echo "=== POST /nudge score=40 (below threshold) ==="; curl -s -XPOST "$B/nudge" -d '{"score":40}'
echo "=== POST /nudge bad score ==="; curl -s -XPOST "$B/nudge" -d '{"score":"high"}'
echo "=== POST /nudge score=88 (fires, call fails: no creds) ==="; curl -s -XPOST "$B/nudge" -d '{"score":88,"context":"HRV down 20%"}'
sleep 1
echo "=== POST /nudge score=88 again (cooldown) ==="; curl -s -XPOST "$B/nudge" -d '{"score":88}'
echo "=== POST /test-call (expect 502 elevenlabs_call stage) ==="; curl -s -XPOST "$B/test-call" -d '{"message":"test"}'
echo "=== POST /test-call empty message ==="; curl -s -XPOST "$B/test-call" -d '{}'
echo "=== PATCH /config ==="; curl -s -XPATCH "$B/config" -d '{"stressThreshold":60,"cooldownSeconds":5}'
echo "=== PATCH /config rejects secret ==="; curl -s -XPATCH "$B/config" -d '{"ELEVENLABS_API_KEY":"x"}'
echo "=== GET /status (nudgeCount should be 1) ==="; curl -s "$B/status"
echo "=== unknown route ==="; curl -s "$B/nope"
echo "=== state.json persisted ==="; cat "$DIR/state.json"
echo "=== logs ==="; cat "$DIR/boot.log"
pkill -f "$DIR/health_call_nudger.js" 2>/dev/null
pkill -f "health_call_nudger.js" 2>/dev/null
rm -rf "$DIR"
