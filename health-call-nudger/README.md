# health-call-nudger

Standalone Node.js service that takes a stress score and places an outbound coaching phone call
through the [ElevenLabs Conversational AI Twilio integration](https://elevenlabs.io/docs/api-reference/twilio/outbound-call).

No health-data ingestion, no LLM, no other services — the score is supplied by the caller.

## Setup

```bash
npm install
cp .env.example .env   # fill in the four ElevenLabs values
npm start
```

The service boots and serves `/status` even when the ElevenLabs env vars are missing; it logs a
warning listing what's absent and call attempts fail with `stage: "elevenlabs_call"`.

### Env

| Variable | Purpose |
| --- | --- |
| `ELEVENLABS_API_KEY` | `xi-api-key` for the ElevenLabs API |
| `ELEVENLABS_AGENT_ID` | Conversational AI agent placing the call |
| `ELEVENLABS_PHONE_NUMBER_ID` | ID of the Twilio number linked to the agent |
| `TO_NUMBER` | Destination number, E.164 format |

## Endpoints

`GET /` returns [`api.json`](api.json) with the full contract. Summary:

| Endpoint | Purpose |
| --- | --- |
| `POST /nudge` | `{ score, context }` → calls if `score >= stressThreshold` and no call within `cooldownSeconds`. Returns immediately, calls asynchronously. |
| `POST /test-call` | `{ message }` → places the call directly, bypassing scoring and cooldown. Use to verify the ElevenLabs/Twilio link. |
| `GET /status` | `nudgeCount`, `lastNudgeAt`, `cooldownActive`, readiness. |
| `GET`/`PATCH /config` | Read/update `stressThreshold`, `cooldownSeconds`, `listenerPort` (persisted to `config.json`). Secrets are never exposed here. |

`nudgeCount` and `lastNudgeAt` persist to `state.json` across restarts.

## Process management

```bash
pm2 start ecosystem.config.js
```

## Out of scope (placeholders only)

- health-data ingestion (Terra, Apple Health, wearables) feeding `/nudge`
- LLM-generated coaching copy replacing the message template
- decision-maker / monitor service registration
