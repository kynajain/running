# running

`running` syncs Apple Health exports through pluggable connectors and workers.
The default local sink is JSONL; a Notion sink is included for database
upserts.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
running sync --source synthetic --sink jsonl --since 1d --output running.jsonl
```

The synthetic connector generates a deterministic 400 m-ish track run centred
on London Olympic Stadium (`51.5387, -0.0166`), which makes it suitable for
development without an Apple device.

## Real Apple Health data

Apple Health is iOS-only and has no cloud API. You can provide data through:

1. The Health app's **Export All Health Data**, which creates `export.zip`.
2. The [Health Auto Export](https://www.health-autoexport.com/) app.
3. A third-party bridge such as [Terra](https://tryterra.co/) or
   [Vital](https://www.tryvital.io/).

For an Apple export, pass either the zip, its `export.xml`, or an extracted
directory:

```bash
running sync --source apple_health --export ~/Downloads/export.zip \
  --sink jsonl --since 7d
```

The stress score is an explicit heuristic, not an Apple Health metric. It
blends low HRV SDNN and elevated resting heart rate relative to a rolling
baseline. It should not be interpreted as medical advice.

## Notion

Create an internal Notion integration, copy its token, and share the target
database with that integration. The database should contain these properties:

* `Title` — title
* `Number` — number
* `Date` — date
* `Source` — rich text
* `External ID` — rich text

Set credentials before syncing:

```bash
export NOTION_API_TOKEN=secret_...
export NOTION_DATABASE_ID=...
running sync --source synthetic --sink notion --since 1d
```

The sink queries `External ID` before creating each page, so retries and
re-syncs do not duplicate records. Rate-limited requests respect Notion's
`Retry-After` response header.

## In-app call mode

The runner leg is an in-app ElevenLabs WebRTC/WebSocket conversation and
requires the app to be open on her device. The emergency contact receives an
SMS only; there is no voice call to the contact. A session that connects but
hears nothing is treated as unanswered and escalates.

Set these environment variables:

* `ELEVENLABS_API_KEY`
* `ELEVENLABS_AGENT_ID`
* `APP_TOKEN`
* `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, and
  `CONTACT_PHONE_NUMBER` (optional SMS leg; provide all four together)
* `ESCALATION_DELAY_SECONDS` (optional, default `120`)
* `SESSION_MAX_SECONDS` (optional, default `90`)
* `HOST` (optional, default `127.0.0.1`)
* `PORT` (optional, default `8000`)

Start the token-minting demo server with either:

```bash
running-app
# or
python -m running.app.server
```

The server listens on `127.0.0.1` unless `HOST` says otherwise. All API routes
require the configured bearer `APP_TOKEN`, while `/healthz` is public.

The ElevenLabs API key never reaches the browser. The server uses it to mint
short-lived conversation tokens or signed URLs, then returns only those
temporary credentials to the app. The API routes require the configured
`APP_TOKEN`; `/healthz` is public. The emergency-contact SMS is sent through
Twilio after the escalation delay if the runner has not explicitly
acknowledged or produced a non-empty user transcript turn.

The Twilio SMS leg is optional for demos. If it is not configured, escalation
finishes as a dry run, logs the exact SMS body it would have sent, and exposes
`dry_run: true` in the incident view. Partial SMS configuration is rejected at
startup.

The in-memory incident state machine, credential minting, and mocked HTTP
tests have been exercised locally. The in-app runner conversation was also
exercised against a real ElevenLabs account in the browser. No live Twilio SMS
has been sent by this code.
