# health-call-nudger

A safety check-in tool for runners. It receives biometric data from
[Terra](https://docs.tryterra.co) webhooks (Oura, via Terra's server-side integration) and places a
calm outbound phone call through the
[ElevenLabs Conversational AI Twilio integration](https://elevenlabs.io/docs/api-reference/twilio/outbound-call)
when stress indicators spike during or around a run. If the runner does not answer, it can
optionally call a nominated emergency contact.

## Read this before demoing it

- **This is a check-in tool, not an emergency response system.** Do not present it as one.
- **Escalation calls a third party automatically.** That person must consent in advance to being
  phoned by an automated system about someone else's wearable data. Escalation is **off by
  default** (`escalationEnabled: false`) and must be switched on deliberately.
- **Terra's stress score is a recalculated daily value, not a live reading.** Escalations can lag
  the underlying event by minutes or more, and must not be treated as real-time emergency
  detection. A call the runner missed does not mean something happened, and something happening
  does not guarantee a call.
- **With escalation off, an unanswered call contacts nobody.** The service records the miss and
  logs it; that is all.

## Setup

```bash
npm install
cp .env.example .env   # fill in the ElevenLabs values, the Terra secret, ADMIN_TOKEN
npm start
```

The service boots and answers `/health` and `/status` even when env vars are missing; it warns at
startup and reports what's absent.

### Env

| Variable | Purpose |
| --- | --- |
| `ELEVENLABS_API_KEY` | `xi-api-key` for the ElevenLabs API |
| `ELEVENLABS_AGENT_ID` | Conversational AI agent that calls the runner |
| `ELEVENLABS_PHONE_NUMBER_ID` | ID of the Twilio number linked to the agent |
| `TO_NUMBER` | The runner's number, E.164 format |
| `TERRA_SIGNING_SECRET` | Terra webhook signing secret, from the Terra dashboard |
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

Terra delivers to a public HTTPS URL, so this service ends up internet-facing. Only three routes
are safe to expose:

| Public | Admin (requires `ADMIN_TOKEN`) |
| --- | --- |
| `POST /webhook/terra`, `GET /health`, `GET /` | `POST /nudge`, `POST /test-call`, `POST /acknowledge`, `PATCH /config` |

The admin routes place real phone calls to the runner and her emergency contact, and change who
gets called. Left open on a public host, `/test-call` alone lets anyone ring either of them
repeatedly. They require an `x-admin-token` header matching `ADMIN_TOKEN` (compared in constant
time); when `ADMIN_TOKEN` is unset they only accept loopback connections. There is no bypass flag.

```bash
curl -XPOST localhost:4300/test-call -H "x-admin-token: $ADMIN_TOKEN" -d '{"message":"test"}'
```

`GET /status` and `GET /config` never return secrets, but they do expose call history — put them
behind the proxy too if the host is public.

### Webhook hardening

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
| `POST /webhook/terra` | Terra receiver. Verifies `terra-signature`, then acknowledges immediately and processes in the background. |
| `POST /nudge` | Manual trigger, `{ score, context }`. Subject to threshold and cooldown. |
| `POST /test-call` | `{ message }` → places the call directly, bypassing scoring and cooldown. |
| `POST /acknowledge` | "I'm fine" — cancels a pending escalation inside its delay window. |
| `GET /health` | Platform health check: uptime and config validity, independent of call state. |
| `GET /status` | `nudgeCount`, `lastNudgeAt`, `lastCallOutcome`, `cooldownActive`, escalation state and recent audit entries. |
| `GET`/`PATCH /config` | Read/update `stressThreshold`, `cooldownSeconds`, `listenerPort`, `escalationEnabled`, `escalationDelaySeconds` (persisted to `config.json`). Secrets are never exposed or accepted here. |

### Terra webhook behaviour

Signature verification follows Terra's own scheme: the `terra-signature` header is
`t=<unix_seconds>,v1=<hex>`, the signed payload is `` `${t}.${rawBody}` `` hashed with HMAC-SHA256,
compared in constant time, within a 300-second window.

Terra resends payloads for the same period as more data arrives, and `data_enrichment` fields are
latest-known — they can come back `null` on a resend. The service keys each period and never lets
a `null` overwrite a score it already has, and calls at most once per period.

### Scoring

`computeStressScore()` reads `data_enrichment.stress` from the payload and returns 0-100, or `null`
to skip. Nothing else is inferred.

Deliberately **not** implemented: exertion-aware scoring. Raw heart rate is not a valid stress
signal for a runner — elevated HR is the expected state mid-run. Real logic needs HRV against a
personal baseline, or HR relative to pace. Do not add raw HR thresholds.

### Call outcomes and escalation

Each call is followed by polling the ElevenLabs conversation until it reaches a terminal state, and
recorded in `lastCallOutcome` as `answered`, `unanswered`, `failed` or `unknown`, with the `stage`
reached so a failure names the layer that broke.

When a check-in call to the runner goes unanswered and `escalationEnabled` is true:

1. The escalation window is claimed immediately, then the service waits `escalationDelaySeconds`
   (default 120).
2. It re-checks whether the runner turned up in the meantime — either she picked up late, or
   `POST /acknowledge` was called.
3. Only if she is still unreachable does it place **one** call to `EMERGENCY_CONTACT_NUMBER`, using
   the separate `ELEVENLABS_ESCALATION_AGENT_ID` agent, because the script for a third party is not
   the runner's script and the personas must not be shared.

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

`npm run tunnel` prints the public URL and the exact
`https://…/webhook/terra` callback to paste into the Terra dashboard.

**The tunnel URL changes every time the tunnel restarts** unless you configure a reserved ngrok
domain or a named Cloudflare tunnel. Each time it changes you must update the callback URL in the
Terra dashboard, or deliveries will fail silently.

### Deployment

```bash
cp .env.example .env   # fill in, including ADMIN_TOKEN
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

Then set the Terra dashboard callback to `https://<your-host>/webhook/terra`.

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
