# RunningHealth (iOS)

A SwiftUI app that reads Apple Health through HealthKit and feeds the `running`
sync pipeline. HealthKit is the only way to reach Apple Health data — there is
no cloud API — so this target must be built with Xcode and run on a real
iPhone; the simulator has no Health database.

## Build

The project is generated from `project.yml`, so no `.xcodeproj` is committed:

```bash
brew install xcodegen
cd ios && xcodegen generate && open RunningHealth.xcodeproj
```

Then, once per developer account:

1. Select the `RunningHealth` target › Signing & Capabilities and set your team.
   The bundle identifier defaults to `ai.devin.running.RunningHealth`.
2. Confirm the **HealthKit** capability is present with **Background Delivery**
   enabled. It is declared in `RunningHealth/RunningHealth.entitlements`, but
   the capability also has to exist on the App ID in the developer portal.
3. Run on a device signed into iCloud with Health data.

## What it reads

`HealthMetric` mirrors `running.models.Metric`, and the unit strings match the
ones in Apple's XML export, so records produced here are indistinguishable
downstream from records imported out of an `export.zip`:

| App | Backend metric | Unit |
| --- | --- | --- |
| Heart rate | `heart_rate` | `count/min` |
| HRV SDNN | `hrv_sdnn` | `ms` |
| Resting heart rate | `resting_heart_rate` | `count/min` |
| Respiratory rate | `respiratory_rate` | `count/min` |
| Active energy | `active_energy` | `kcal` |

Workouts are read with their routes (`HKWorkoutRoute` → `GeoPoint`).

Reads use `HKAnchoredObjectQueryDescriptor` with anchors persisted in
`UserDefaults`, so each sync only pulls what changed. The first run has no
anchor and backfills 30 days.

HealthKit deliberately hides read authorization: a declined type returns an
empty result rather than an error, so an empty sync is not a failure.

## Where the data goes

`Settings` takes an **https** endpoint (plain http is rejected) and an optional
bearer token, which is stored in the keychain. Records are POSTed as NDJSON,
one `{"type": "sample" | "workout", "record": {...}}` per line, retrying 429
and 5xx with `Retry-After`. Records stay in `pending` until the endpoint
accepts them, so advancing an anchor can never silently drop data.

With no endpoint configured, **Export** writes the same NDJSON to a file you
can hand to the backend directly:

```bash
running sync --source ndjson --export batch.ndjson --sink jsonl --since 7d
```

Background delivery is opt-in in Settings and wakes the app at most hourly.
