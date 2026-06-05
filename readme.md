
auto import:
- using https://www.healthyapps.dev/apps/health-auto-export/ (paid after 1wk trial)

manual import:
- the apple health native export hangs for 8+ hours for me. i have a lot of HR samples.
- using https://www.healthyapps.dev/apps/health-auto-export/ (free may support manual export?)
- metrics
- workouts
  - date range all
  - format JSON
  - v2
  - select only workouts.
  - include GPX (this will include gpx files but also populates the route data into the JSON, which is what we use)
  - include metrics.
  - time grouping to minutes (export is waaay slower if you set to seconds).
  - this completes for me in a couple mins for several hundred workouts
  - upload the resulting .zip on the dashboard. i synced it to my laptop via airdrop.
    - (the upload goes faster if you unzip, and then re-zip just the json. the original zip doesn't seem to actually be compressed, and the gpx files are redundant to the json route data).
