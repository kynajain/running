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

1. The `RunningHealth` iOS app in [`ios/`](ios/README.md), which reads
   HealthKit directly and pushes NDJSON.
2. The Health app's **Export All Health Data**, which creates `export.zip`.
3. The [Health Auto Export](https://www.health-autoexport.com/) app.
4. A third-party bridge such as [Terra](https://tryterra.co/) or
   [Vital](https://www.tryvital.io/).

For an Apple export, pass either the zip, its `export.xml`, or an extracted
directory:

```bash
running sync --source apple_health --export ~/Downloads/export.zip \
  --sink jsonl --since 7d
```

Batches from the iOS app are NDJSON, one
`{"type": "sample" | "workout", "record": {...}}` per line:

```bash
running sync --source ndjson --export batch.ndjson --sink jsonl --since 7d
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

## Stress alerts over SMS

The `twilio` sink texts you when a derived stress score crosses a threshold. It
sends at most one message per sync — for the worst score in the batch — and
keeps a cooldown in `.running-twilio-state.json` so an hourly sync cannot turn
a bad day into a stream of texts.

```bash
export TWILIO_ACCOUNT_SID=AC...
export TWILIO_AUTH_TOKEN=...
export TWILIO_FROM_NUMBER=+15005550006
export TWILIO_TO_NUMBER=+447700900123
running sync --source ndjson --export batch.ndjson --sink twilio \
  --since 1d --stress-threshold 75
```

Both numbers must be E.164. On a Twilio trial account the destination has to be
a number you verified in the console, and messages are prefixed with a trial
notice. The alert is a nudge based on a heuristic, not medical advice.

## Accident and stress detection

`running.detection` holds the sensor-fusion and response layer that an app
drives with live sensor data.

Detection never compares a sensor against a fixed cut-off. `signals` regresses
each metric on movement intensity derived from the accelerometer, then scores
how far the latest reading sits from its activity-adjusted expectation: heart
rate elevated beyond what the effort explains, HRV suppressed more steeply than
exertion predicts, and — where hardware provides it — phasic skin-conductance
bursts rather than tonic level, which rises with thermoregulatory sweat.
`fusion` renormalises the weights over whichever detectors reported and refuses
to fire on a single signal. Audio-based symptoms such as cough detection are
deliberately absent; the signal-to-noise is not good enough to escalate on.

Adding a sensor is implementing `SignalDetector` and calling
`register_detector`; the fusion model does not change.

`impact.detect_impact` reports a fall only when a hard impact, the deceleration
preceding it, an orientation change across it and a post-impact window of
near-zero movement all hold together.

`response.ResponseMachine` is the pure state machine behind the escalation:

```
idle -> confirming -> consenting -> recording -> alarm
```

A crossed threshold opens an "Are you okay?" prompt with a 10-20s countdown
that a single dismissal closes. Silence is treated as incapacitation and raises
the alarm. Confirmed distress offers recording only if the user opted in during
setup — never a live decision under stress, which keeps two-party audio consent
informed — and raises the alarm either way. The machine performs no I/O: it
returns effects (`ShowConfirmationPrompt`, `StartRecording`, `RaiseAlarm`, ...)
for the caller to execute, so location lookup, cloud upload and contact
notification stay replaceable.

Apple hardware constrains what can be fed in: HealthKit exposes no
electrodermal type and no continuous beat-to-beat intervals (so RMSSD and HF
power are unavailable — SDNN is what you get), and iOS will not start camera
capture from the background.
