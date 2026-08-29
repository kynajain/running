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

## Runner safety voice escalation

The `alert` command is a mocked-HTTP-ready escalation path. Dry-run mode is
safe for demos and requires no credentials:

```bash
running alert --lat 51.5387 --lon -0.0166 --dry-run
```

Live mode requires these environment variables:

* `ELEVENLABS_API_KEY`
* `ELEVENLABS_RUNNER_AGENT_ID`
* `ELEVENLABS_CONTACT_AGENT_ID`
* `ELEVENLABS_AGENT_PHONE_NUMBER_ID`
* `TWILIO_ACCOUNT_SID`
* `TWILIO_AUTH_TOKEN`
* `TWILIO_FROM_NUMBER`
* `RUNNER_PHONE_NUMBER`
* `EMERGENCY_CONTACT_PHONE_NUMBER`

Before enabling live mode, a human must create the runner and emergency
contact agents in ElevenLabs, import the Twilio number to obtain the
ElevenLabs Phone Number ID, verify the caller ID or buy a number, and confirm
the account plan allows outbound calling. No live call has ever been made from
this code.

The ElevenLabs client uses `POST /v1/convai/twilio/outbound-call` and polls
`GET /v1/convai/conversations/{conversation_id}`. The documented conversation
statuses are `initiated`, `in-progress`, `processing`, `done`, and `failed`;
unrecognised values map to `unknown`.

## In-app call mode

The runner leg can instead use an ElevenLabs in-app WebRTC/WebSocket
conversation, with no runner phone number required. Set these environment
variables:

* `ELEVENLABS_API_KEY`
* `ELEVENLABS_RUNNER_AGENT_ID`
* `ELEVENLABS_CONTACT_AGENT_ID`

Start the token-minting demo server with either:

```bash
running-app
# or
python -m running.app.server
```

The ElevenLabs API key never reaches the browser. The server uses it to mint
short-lived conversation tokens or signed URLs, then returns only those
temporary credentials to the app. The emergency-contact leg still requires
real telephony configuration because the contact does not have the app.
