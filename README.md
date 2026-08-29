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
