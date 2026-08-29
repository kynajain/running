#!/usr/bin/env bash
# Local smoke test: boots the service on a scratch dir and exercises every endpoint,
# including Terra signature verification and resend idempotency.
set -u
PORT=4399
SECRET=smoke_signing_secret
TOKEN=smoke_admin_token
DEVTOKEN=smoke_device_token
DIR=$(mktemp -d)
cp health_call_nudger.js api.json package.json "$DIR/"
ln -s "$PWD/node_modules" "$DIR/node_modules"
printf '{"stressThreshold":75,"cooldownSeconds":1800,"listenerPort":%s,"escalationEnabled":false,"escalationDelaySeconds":1}\n' "$PORT" > "$DIR/config.json"
B="http://127.0.0.1:$PORT"
A=(-H "x-admin-token: $TOKEN")
D=(-H "x-device-token: $DEVTOKEN")

# Signs a body the way Terra does: HMAC-SHA256 over "<unix_seconds>.<raw body>".
sign() {
  TERRA_SIGNING_SECRET="$SECRET" node -e '
    const crypto = require("crypto");
    const body = process.argv[1];
    const t = process.argv[2] || Math.floor(Date.now() / 1000);
    const v1 = crypto.createHmac("sha256", process.env.TERRA_SIGNING_SECRET)
      .update(`${t}.${body}`, "utf8").digest("hex");
    process.stdout.write(`t=${t},v1=${v1}`);
  ' "$1" "${2:-}"
}

post_terra() {
  local body="$1" ts="${2:-}"
  curl -s -w 'HTTP %{http_code}\n' -XPOST "$B/webhook/terra" \
    -H "terra-signature: $(sign "$body" "$ts")" -H 'Content-Type: application/json' -d "$body"
}

echo "=== boot with NO call env (expect warnings, service still up) ==="
(cd "$DIR" && env -u ELEVENLABS_API_KEY TERRA_SIGNING_SECRET="$SECRET" ADMIN_TOKEN="$TOKEN" DEVICE_TOKEN="$DEVTOKEN" node health_call_nudger.js > boot.log 2>&1 &)
sleep 1
cat "$DIR/boot.log"

echo "=== GET / ===";        curl -s "$B/" | head -4
echo "=== GET /health ===";  curl -s "$B/health"
echo "=== GET /status without a token (expect 401; it carries her location) ==="
curl -s "$B/status"
echo "=== GET /status ===";  curl -s "$B/status" "${A[@]}"
echo "=== GET /config ===";  curl -s "$B/config" "${A[@]}"

echo "=== admin routes without a token (expect 401 unauthorised) ==="
for route in nudge test-call acknowledge; do
  printf '  POST /%s -> ' "$route"; curl -s -XPOST "$B/$route" -d '{}' | tr -d '\n'; echo
done
printf '  PATCH /config -> '; curl -s -XPATCH "$B/config" -d '{}'
echo "=== admin route with a WRONG token (expect 401) ==="
curl -s -XPOST "$B/test-call" -H 'x-admin-token: nope' -d '{"message":"x"}'

echo "=== POST /ingest with NO device token (expect 401) ==="
curl -s -XPOST "$B/ingest" -d '{"stress":88}'
echo "=== POST /ingest location only, no stress (expect no_stress_value) ==="
curl -s -XPOST "$B/ingest" "${D[@]}" -d '{"heartRate":171,"location":{"lat":51.5072,"lng":-0.1276,"accuracyMeters":8}}'
echo "=== POST /ingest bad location (expect 400) ==="
curl -s -XPOST "$B/ingest" "${D[@]}" -d '{"location":{"lat":999,"lng":0}}'
echo "=== POST /ingest stress 40 (expect below_threshold) ==="
curl -s -XPOST "$B/ingest" "${D[@]}" -d '{"stress":40}'
echo "=== POST /ingest stress 88 (expect nudge ok, call attempted) ==="
curl -s -XPOST "$B/ingest" "${D[@]}" -d '{"stress":88,"context":"mid-run","location":{"lat":51.5072,"lng":-0.1276}}'
sleep 1
echo "=== POST /ingest stress 92 again (expect ok: the failed call handed its cooldown slot back) ==="
curl -s -XPOST "$B/ingest" "${D[@]}" -d '{"stress":92}'
sleep 1

echo "=== POST /webhook/terra with NO signature (expect 401) ==="
curl -s -o /dev/null -w "%{http_code}\n" -XPOST "$B/webhook/terra" -d '{}'
echo "=== POST /webhook/terra with BAD signature (expect 401) ==="
curl -s -XPOST "$B/webhook/terra" -H 'terra-signature: t=1,v1=deadbeef' -d '{}'
echo "=== POST /webhook/terra with STALE timestamp (expect 401) ==="
post_terra '{"type":"activity","user":{"user_id":"u1"},"data":[]}' 1000000000

echo "=== POST /webhook/terra activity, no stress enrichment (expect 200, skipped) ==="
post_terra '{"type":"activity","user":{"user_id":"u1","provider":"OURA"},"data":[{"metadata":{"start_time":"2026-08-29T09:00:00Z"},"heart_rate_data":{"summary":{"avg_hr_bpm":168}}}]}'

echo "=== POST /webhook/terra daily, stress 88 (expect 200, call attempted) ==="
post_terra '{"type":"daily","user":{"user_id":"u1","provider":"OURA"},"data":[{"metadata":{"start_time":"2026-08-29T00:00:00Z"},"data_enrichment":{"stress":88}}]}'
sleep 1

echo "=== resend SAME period with stress null (must keep 88, not overwrite) ==="
post_terra '{"type":"daily","user":{"user_id":"u1","provider":"OURA"},"data":[{"metadata":{"start_time":"2026-08-29T00:00:00Z"},"data_enrichment":{"stress":null}}]}'

echo "=== POST /webhook/terra unknown type (ignored) ==="
post_terra '{"type":"sleep","user":{"user_id":"u1"},"data":[{"metadata":{"start_time":"x"},"data_enrichment":{"stress":99}}]}'

echo "=== oversized webhook body (expect 413) ==="
head -c 4000000 /dev/zero | tr '\0' 'a' > "$DIR/big.txt"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -XPOST "$B/webhook/terra" \
  -H 'terra-signature: t=1,v1=deadbeef' --data-binary "@$DIR/big.txt"

echo "=== webhook rate limit (61 unsigned requests, expect a 429 at the end) ==="
for _ in $(seq 1 61); do
  curl -s -o /dev/null -w '%{http_code} ' -XPOST "$B/webhook/terra" -d '{}'
done | tail -c 40; echo

echo "=== POST /nudge score=40 (below threshold) ==="; curl -s -XPOST "$B/nudge" "${A[@]}" -d '{"score":40}'
echo "=== POST /nudge bad score ===";                 curl -s -XPOST "$B/nudge" "${A[@]}" -d '{"score":"high"}'
echo "=== POST /nudge score=88 (no credentials here, so no call is ever accepted and the slot stays free) ==="; curl -s -XPOST "$B/nudge" "${A[@]}" -d '{"score":88}'
echo "=== POST /acknowledge ===";                    curl -s -XPOST "$B/acknowledge" "${A[@]}"
echo "=== POST /test-call (expect 502, stage elevenlabs_call) ==="; curl -s -XPOST "$B/test-call" "${A[@]}" -d '{"message":"test"}'
echo "=== POST /test-call empty message ===";        curl -s -XPOST "$B/test-call" "${A[@]}" -d '{}'
echo "=== PATCH /config ===";                        curl -s -XPATCH "$B/config" "${A[@]}" -d '{"stressThreshold":60,"cooldownSeconds":5}'
echo "=== PATCH /config rejects secret ===";         curl -s -XPATCH "$B/config" "${A[@]}" -d '{"TERRA_SIGNING_SECRET":"x"}'
echo "=== PATCH /config escalationEnabled must be boolean ==="; curl -s -XPATCH "$B/config" "${A[@]}" -d '{"escalationEnabled":"yes"}'
echo "=== PATCH /config fractional listenerPort (expect 400) ==="; curl -s -XPATCH "$B/config" "${A[@]}" -d '{"listenerPort":4300.5}'
echo "=== PATCH /config listenerPort above 65535 (expect 400) ==="; curl -s -XPATCH "$B/config" "${A[@]}" -d '{"listenerPort":70000}'
echo "=== GET /status (lastCallOutcome should name the failing stage) ==="; curl -s "$B/status" "${A[@]}"
echo "=== unknown route ===";                        curl -s "$B/nope"
echo "=== state.json ===";                           cat "$DIR/state.json"
pkill -f "health_call_nudger.js" > /dev/null 2>&1

echo
echo "=== escalation gating (in-process, no calls placed) ==="
(cd "$DIR" && TERRA_SIGNING_SECRET="$SECRET" node -e '
  const svc = require("./health_call_nudger.js");
  (async () => {
    svc.loadConfig();
    svc.loadState();

    // Escalation disabled: an unanswered call must contact nobody.
    console.log("disabled ->", await svc.handleUnansweredCall({ conversationId: "c1" }, { score: 88 }));

    // Enabled but unconfigured: audited as failed, still nobody called.
    const fs = require("fs");
    const cfg = JSON.parse(fs.readFileSync("config.json", "utf8"));
    cfg.escalationEnabled = true;
    fs.writeFileSync("config.json", JSON.stringify(cfg));
    svc.loadConfig();
    console.log("unconfigured ->", (await svc.handleUnansweredCall({ conversationId: "c1" }, { score: 88 })).verdict);

    process.env.ELEVENLABS_ESCALATION_AGENT_ID = "agent_esc";
    process.env.EMERGENCY_CONTACT_NUMBER = "+15550000000";
    process.env.EMERGENCY_CONTACT_NAME = "Sam";

    // Acknowledged during the delay: cancelled, nobody called.
    const pending = svc.handleUnansweredCall({ conversationId: null }, { score: 88 });
    svc.acknowledge("smoke");
    console.log("acknowledged ->", (await pending).verdict);

    // Switching escalation off during the delay must cancel the pending call.
    const inflight = svc.handleUnansweredCall({ conversationId: null }, { score: 89 });
    cfg.escalationEnabled = false;
    fs.writeFileSync("config.json", JSON.stringify(cfg));
    svc.loadConfig();
    console.log("disabled mid-delay ->", (await inflight).verdict);
    cfg.escalationEnabled = true;
    fs.writeFileSync("config.json", JSON.stringify(cfg));
    svc.loadConfig();

    // Second escalation inside the same cooldown window must be refused.
    const first = await svc.handleUnansweredCall({ conversationId: null }, { score: 91 });
    const second = await svc.handleUnansweredCall({ conversationId: null }, { score: 92 });
    console.log("first ->", first.verdict, "| second ->", second.verdict, second.reason);

    // A fresh fix is spoken with its age; a stale one is dropped entirely.
    console.log("fresh location shared ->", Boolean(svc.shareableLocation().location));
    console.log("escalation script ->", svc.buildEscalationMessage(88, "Sam"));

    svc.recordIngest({ location: { lat: 51.5, lng: -0.12, at: new Date(Date.now() - 3600e3).toISOString() } });
    console.log("stale location shared ->", Boolean(svc.shareableLocation().location));
    console.log("stale script mentions position ->", /latitude/.test(svc.buildEscalationMessage(88, "Sam")));
    process.exit(0);
  })();
')

echo "=== logs ===";                                 cat "$DIR/boot.log"
rm -rf "$DIR"
