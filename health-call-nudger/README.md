# health-call-nudger

A safety check-in tool for runners. The runner's app pushes health data and its live location to
`POST /ingest`, and when stress indicators spike the service places a calm outbound phone call
through the
[ElevenLabs Conversational AI Twilio integration](https://elevenlabs.io/docs/api-reference/twilio/outbound-call).
If she does not answer, it can optionally call a nominated emergency contact and read out her last
known position.

`POST /webhook/terra` still exists as a legacy path for [Terra](https://docs.tryterra.co)-delivered
Oura data, and works exactly as before; nothing needs to be configured for it if you are not using
it.

## Read this before demoing it

- **This is a check-in tool, not an emergency response system.** Do not present it as one.
- **Escalation calls a third party automatically.** That person must consent in advance to being
  phoned by an automated system about someone else's wearable data. Escalation is **off by
  default** (`escalationEnabled: false`) and must be switched on deliberately.
- **Not real-time emergency detection.** Readings and location fixes are as fresh as the app's last
  upload, which is not the same as live: a phone in a pocket with a poor signal reports late or not
  at all. On the legacy Terra path this is worse — Terra's stress score is a recalculated daily
  value. A call the runner missed does not mean something happened, and something happening does
  not guarantee a call.
- **Location is spoken as "where her phone was", with its age**, and a fix older than
  `locationMaxAgeSeconds` (900) is not read out at all. Set `shareLocationWithContact: false` to
  keep it out of the call entirely.
- **With escalation off, an unanswered call contacts nobody.** The service records the miss and
  logs it; that is all.

## Setup

```bash
npm install
cp .env.example .env   # ElevenLabs values, DEVICE_TOKEN, ADMIN_TOKEN
npm start
```

The service boots and answers `/health` even when env vars are missing; it warns at
startup and reports what's absent.

### Env

| Variable | Purpose |
| --- | --- |
| `ELEVENLABS_API_KEY` | `xi-api-key` for the ElevenLabs API |
| `ELEVENLABS_AGENT_ID` | Conversational AI agent that calls the runner |
| `ELEVENLABS_PHONE_NUMBER_ID` | ID of the Twilio number linked to the agent |
| `TO_NUMBER` | The runner's number, E.164 format |
| `DEVICE_TOKEN` | The app's credential for `POST /ingest` |
| `TERRA_SIGNING_SECRET` | Legacy Terra webhook signing secret, only if you use that path |
| `ADMIN_TOKEN` | Shared secret for the admin endpoints (see below) |
| `ELEVENLABS_ESCALATION_AGENT_ID` | Separate agent used for the escalation call |
| `EMERGENCY_CONTACT_NUMBER` | Contact's number, E.164 format |
| `EMERGENCY_CONTACT_NAME` | Used in the escalation script |
| `BIND_ADDRESS` | Listen address, default `127.0.0.1`; `0.0.0.0` in the container |
| `STATE_PATH` | Override the state file location (used by the container volume) |
| `TRUST_PROXY` | `1` when a proxy in front sets `X-Forwarded-For` |

The 90-second call cap and the spoken opening line are sent as `conversation_config_override`,
which ElevenLabs only applies if the matching overrides are enabled on the agent in the dashboard.

## Exposure and auth

The app posts from the runner's phone over the internet, so this service is internet-facing:

| Public | Device (`x-device-token`) | Admin (`x-admin-token`) |
| --- | --- | --- |
| `GET /health`, `GET /`, legacy `POST /webhook/terra` (signature-checked) | `POST /ingest` | `POST /nudge`, `POST /test-call`, `POST /acknowledge`, `GET /status`, `GET /config`, `PATCH /config` |

`DEVICE_TOKEN` is deliberately not `ADMIN_TOKEN`: the phone's credential ships inside an app and
can leak, and revoking it must not lock the operator out (or vice versa). While `DEVICE_TOKEN` is
unset, `/ingest` rejects everything. Note that a leaked device token lets someone post fake
readings and fake positions, which is why it is not the same secret that can place calls.

The admin routes place real phone calls to the runner and her emergency contact, and change who
gets called. Left open on a public host, `/test-call` alone lets anyone ring either of them
repeatedly. They require an `x-admin-token` header matching `ADMIN_TOKEN` (compared in constant
time); when `ADMIN_TOKEN` is unset they only accept loopback connections, and if `TRUST_PROXY=1`
they are closed outright — a reverse proxy on the same host makes an internet request arrive from
127.0.0.1 like any local one, so loopback stops meaning "local". There is no bypass flag. `/health`
reports a missing `ADMIN_TOKEN` as a problem whenever the deployment is proxied or non-loopback.

```bash
curl -XPOST localhost:4300/test-call -H "x-admin-token: $ADMIN_TOKEN" -d '{"message":"test"}'
```

`GET /status` and `GET /config` need the same token: they never return secrets, but `/status`
carries the runner's coordinates, her readings and her call history. Platform and load-balancer
probes use `GET /health`, which carries none of that.

### Ingest and webhook hardening

`POST /ingest` is authenticated before anything is stored, capped at 64 KB, and shares the per-IP
rate limit below. Only `stress` can trigger a call; `heartRate` and `hrv` are stored for inspection
and never scored (see Scoring).

```bash
curl -XPOST https://your-host/ingest -H "x-device-token: $DEVICE_TOKEN" -d '{
  "stress": 88,
  "heartRate": 171,
  "context": "mid-run",
  "location": { "lat": 51.5072, "lng": -0.1276, "accuracyMeters": 8, "at": "2026-08-29T14:31:00Z" }
}'
```

`location.at` is the phone's own fix time and matters: it is what decides whether the position is
fresh enough to read out on an escalation call. Send it.

On the legacy Terra path:

- Signature verification is strict and runs on the raw body before any parsing. Unsigned, stale
  (outside 300s) or non-matching requests get `401` and never reach the parser.
- Bodies over 3 MB are rejected with `413` rather than buffered, by `Content-Length` and again
  while streaming.
- Per-IP rate limit of 60 requests/minute, `429` above that. Behind a proxy this needs
  `TRUST_PROXY=1` to see real client IPs.
- Every rejection is logged with the source IP and reason.

## Endpoints

`GET /` serves [`api.json`](api.json) with the full contract. Summary:

| Endpoint | Purpose |
| --- | --- |
| `POST /ingest` | Primary data path. App-pushed health data and location; a `stress` value at or above the threshold triggers a check-in call. |
| `POST /webhook/terra` | Legacy Terra receiver. Verifies `terra-signature`, then acknowledges immediately and processes in the background. |
| `POST /nudge` | Manual trigger, `{ score, context }`. Subject to threshold and cooldown. |
| `POST /test-call` | `{ message }` → places the call directly, bypassing scoring and cooldown. |
| `POST /acknowledge` | "I'm fine" — cancels a pending escalation inside its delay window. |
| `GET /health` | Platform health check: uptime and config validity, independent of call state. |
| `GET /status` | `nudgeCount`, `lastNudgeAt`, `lastCallOutcome`, `cooldownActive`, `lastVitals`, `lastLocation` (with age), escalation state and recent audit entries. |
| `GET`/`PATCH /config` | Read/update `stressThreshold`, `cooldownSeconds`, `listenerPort`, `escalationEnabled`, `escalationDelaySeconds` (persisted to `config.json`). Secrets are never exposed or accepted here. |

### Legacy Terra webhook behaviour

Signature verification follows Terra's own scheme: the `terra-signature` header is
`t=<unix_seconds>,v1=<hex>`, the signed payload is `` `${t}.${rawBody}` `` hashed with HMAC-SHA256,
compared in constant time, within a 300-second window.

Terra resends payloads for the same period as more data arrives, and `data_enrichment` fields are
latest-known — they can come back `null` on a resend. The service keys each period and never lets
a `null` overwrite a score it already has, and calls at most once per period.

### Scoring

The app decides what "stress" means for its own signals and sends the number; the service takes it
and nothing else (`computeIngestStressScore()`). On the legacy path, `computeStressScore()` reads
Terra's `data_enrichment.stress`. Either way `null` means skip.

Deliberately **not** implemented: exertion-aware scoring. Raw heart rate is not a valid stress
signal for a runner — elevated HR is the expected state mid-run. Real logic needs HRV against a
personal baseline, or HR relative to pace. Do not add raw HR thresholds.

### Call outcomes and escalation

Each call is followed by polling the ElevenLabs conversation until it reaches a terminal state, and
recorded in `lastCallOutcome` as `answered`, `unanswered`, `failed` or `unknown`, with the `stage`
reached so a failure names the layer that broke. Only a terminal ElevenLabs status can produce
`unanswered`: if polling runs out of time or the API becomes unreadable, the outcome is `unknown`,
which never escalates. "We could not find out" is not the same as "she did not pick up", and only
one of the two is allowed to ring a third party.

The `cooldownSeconds` slot is claimed before dialling so concurrent payloads cannot stack up calls,
and handed back only when we know no call was placed — bad configuration, or an outright rejection
from ElevenLabs. A timeout keeps the slot, since the request may have landed and a retry would ring
her twice. Terra periods work the same way: a period marked called is a reservation until a call is
accepted, so a failure that never reached ElevenLabs leaves the next resend free to retry, and a
reservation left unconfirmed by a restart is released at boot.

When a check-in call to the runner goes unanswered and `escalationEnabled` is true:

1. The escalation window is claimed immediately, then the service waits `escalationDelaySeconds`
   (default 120).
2. It re-checks `escalationEnabled`, so switching escalation off during the delay cancels the
   pending call instead of letting a timer that started earlier dial anyway.
3. It re-checks whether the runner turned up in the meantime — either she picked up late, or
   `POST /acknowledge` was called.
4. Only if she is still unreachable does it place **one** call to `EMERGENCY_CONTACT_NUMBER`, using
   the separate `ELEVENLABS_ESCALATION_AGENT_ID` agent, because the script for a third party is not
   the runner's script and the personas must not be shared.

If `shareLocationWithContact` is on and the app's last fix is newer than `locationMaxAgeSeconds`,
that call also states the position and how old it is, framed as where her phone was rather than
where she is. A stale fix is dropped rather than read out; `/status` shows
`escalation.locationWouldBeShared` so you can see which it would be right now.

At most one escalation happens per `cooldownSeconds` window, however many Terra payloads arrive for
the same period. A missed escalation call does not escalate further.

The escalation script is deliberately flat: an automated check-in, an elevated wearable reading,
no answer, and that this may well be nothing. It does not claim an emergency has been confirmed and
does not tell the contact to call emergency services.

Every attempt is audited — including the ones that never dial — in `state.escalationLog` and as a
`stage: "escalation"` log line, with timestamp, triggering score, and verdict (`pending`, `called`,
`cancelled`, `skipped`, `failed`, `outcome`). The last ten are visible on `/status`.

`nudgeCount`, `lastNudgeAt`, `lastCallOutcome`, escalation state and the audit log persist to a
gitignored `state.json` across restarts.

## Making it reachable from Terra

### Local development

```bash
npm start            # binds 127.0.0.1 by default
npm run tunnel       # ngrok, or cloudflared as a fallback
```

`npm run tunnel` prints the public URL — point the app's ingest base URL at it (and, on the legacy
path, paste the `https://…/webhook/terra` callback into the Terra dashboard).

**The tunnel URL changes every time the tunnel restarts** unless you configure a reserved ngrok
domain or a named Cloudflare tunnel. Each time it changes you must update the callback URL in the
Terra dashboard, or deliveries will fail silently.

### Deployment

```bash
cp .env.example .env   # fill in, including DEVICE_TOKEN and ADMIN_TOKEN
docker compose up -d --build
```

The image runs as the non-root `node` user, `.env` is mounted rather than baked in (and
`.dockerignore` excludes it from the build context), `logs/` is bind-mounted, and `state.json`
lives on a named volume via `STATE_PATH` so it survives restarts and rebuilds.

Compose publishes the port on `127.0.0.1` only. **Terra requires HTTPS**, so put a TLS terminator
in front:

- **Small VPS**: Caddy or nginx on the host, proxying `443` to `127.0.0.1:4300`, with a real
  certificate (Caddy gets one automatically). Set `TRUST_PROXY=1` so the webhook rate limit sees
  real client IPs.
- **Railway / Render / Fly**: deploy the Dockerfile directly; the platform terminates TLS and sets
  `X-Forwarded-For`, so set `BIND_ADDRESS=0.0.0.0`, `TRUST_PROXY=1`, and `PORT`-equivalent
  `listenerPort` to match what the platform expects. Point the platform's health check at
  `/health`. Set every secret, including `ADMIN_TOKEN`, in the platform's env config — never in the
  image.

Then point the app at `https://<your-host>/ingest` (and, if you still use it, set the Terra
dashboard callback to `https://<your-host>/webhook/terra`).

### PM2 (no container)

```bash
pm2 start ecosystem.config.js   # restart on crash, logs to logs/
```

## Out of scope

- Apple Health and Terra's Mobile SDK — Apple Health has no web API and would require an on-device
  app; the project has decided against building one. All payloads are assumed to arrive from Oura
  via Terra's server-side integration.
- LLM-generated coaching copy; both spoken scripts are templates.

## Local checks

```bash
npm run smoke   # boots on a scratch dir and exercises every endpoint, auth and skip path
```
