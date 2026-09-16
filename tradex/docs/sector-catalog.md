# Full observed sector directory

The desktop page keeps the existing reviewed defense and offense observations and
adds a searchable directory of every industry/concept identity observed in the
Collector's persisted rotation snapshots. This is `sector_catalog.v1`; it does
not claim that the upstream catalog covers every market theme. Names absent from
the source cannot be invented. Expected market-wide count remains unverified.

## Ownership and identity

The existing Collector auxiliary loop materializes the directory. The additional
`sector_catalog_days` and `sector_catalog_curves` tables live in the existing
`TRADEX_ROTATION_DB` database. Web only opens read-only connections. No new quote
requests, service, process, provider, or lake dependencies are introduced.

Identity is a stable digest of normalized source family, taxonomy and source board
code. Names are display metadata; renames preserve aliases. Different sources or
taxonomies are never merged by a similar name. Legacy role membership uses the
same exact reviewed identity/alias rules as the existing two charts. Other boards
remain unclassified. A missing board remains visible as missing, not delisted.

## Discovery and history

All observed same-day boards retain real minute points regardless of hot status.
Two distinct effective provider timestamps satisfying a discovery rule confirm a
hotspot. Initial rules are a gain of at least 2% in the top same-taxonomy decile,
or positive estimated main net flow with flow ratio in that taxonomy's top decile.
These rules are observation signals, not trade recommendations; turnover and
limit-up diffusion are not claimed as inputs to this first version.

Confirmed hotspots remain in the day directory after cooling. Prior-day entries
may remain for directory reconciliation, but their curve points and hot status
are never copied into a new day. Supported new hotspots with missing history join
the existing Collector auxiliary loop, one curve per idle window using the same
cache and provider limiter (2-second queue budget, 4-second request timeout).
They are not registered as required legacy closing targets; quote capture does
not wait for them. This bounded queue can leave visible gaps under source limits.
Old days use archived real points only. They cannot be repaired using today's
provider curve. Missing minutes are explicit, not interpolated or zero-filled.
An explicitly dated backfill may accept a provider response still containing
that exact historical date; every returned point must pass the gateway date
validation. It cannot relabel another day's data.

## Intraday contention and bounded reads

The legacy current and historical chart readers pass their complete reviewed
key set to the gateway, including keys absent from the latest quote snapshot.
The cache and SQLite query filter before decoding curves. Catalog reads retain
their full-universe default; optional curves do not enlarge required close targets.
On a live local store containing 527 curves during backfill, one read measured
0.833 seconds for all 126,480 points versus 0.136 seconds for the 87 persisted
legacy curves (20,880 points). This is a local diagnostic, not market-hours SLA.

The full directory introduces no additional board quote requests, but its
projection still shares the Collector process CPU and database. Optional hot
curve repair uses the existing time window (seconds 22–46 during trading),
queue budget and shared provider limiter; a time window is not proof that a
late main capture has finished. A bulk 1,000-board repair must not be treated as
ordinary intraday work. The September 13 manual run holds the Collector lock on
a non-trading day; managed Collector is restored after its terminal readback.
Actual market-hours CPU/queue contention remains unverified.

The source adapter remembers a successful Eastmoney transport for five minutes,
falls back immediately if it fails, and rechecks the normal order on expiry.
It never relaxes the shared IP limiter. The manual repair uses two bounded
workers and skips already complete persisted curves on resume.

Completeness is relative to the provider's 09:31–11:30 and 13:01–15:00 minute curve,
up to the directory time. The catalog explicitly reports observed, mapped,
unclassified, real-curve, missing-history, missing-latest and unverified-backfill
counts. A source snapshot ending before close is shown with its actual time.

## Read contract and desktop display

`GET /api/market-watch/sector-catalog` returns a manifest without curve points.
`GET /api/market-watch/sector-catalog/trajectory?catalog_revision=…&sector_keys=…`
returns 1–32 exact selected curves. The writer commits the manifest and all curves
in one transaction. A replaced revision returns 409. Reads validate point hashes;
the page validates revision, identities, counts, ordering and time range before
drawing. The directory is independent of the macro snapshot and shows its own
data time. Its revision is not substituted for `source_snapshot_revision` of the
legacy market-watch API.

Search covers the complete directory, including aliases and codes. Table pages
contain 30 entries; curve groups contain at most 12 selected entries, with explicit
group paging. These are display limits, never acquisition or storage filters.
Default tracking adds the 12 strongest currently active hotspots and retains
previously selected same-day hotspots; manually changing selection turns off
automatic additions. Existing defense/offense selection and five-day views stay
independent. This implementation targets desktop only.
The directory calls the same `renderSectorFlowChart` renderer as the original
charts, with independent data, selection and color assignments. It shares the
original trading time axis, smooth lunch bridge, sample-gap threshold, curve
palette, endpoint names and amounts, density-aware scale, point tooltip, legend
click/line click focus and outside-click reset. It adds no synthetic observations.
The existing five-day archive flow is not claimed for this same-day directory.
Clicking the change-percent column toggles descending/ascending order across the
entire filtered result before paging; unavailable changes stay last and equal
values retain their input order.

For an initial materialization, stop the managed Collector, run
`.venv/Scripts/python.exe -m tradex.dashboard.collector_worker --materialize-sector-catalog`,
then start Collector again with `scripts/tradex_control.ps1`. The command takes
the same exclusive Collector lock and performs no provider requests.

## Acceptance on 2026-09-13

- Managed Collector and dashboard reloaded successfully; old market-watch source
  revision stayed `1881849c0ebe0d8406ce071874347affe408f544ec3464869d84a139b3c78cad`.
- Catalog revision `00e569fce5844645e03348a1fb93bb25f5ceb2fa9a8b5a0c00bef7d0d7d11fac`
  covers 1,000 identities observed on 2026-09-11 through 14:52. Of these, 87 map to
  the existing directions, 913 are unclassified, all 1,000 have some real points,
  913 have missing minutes, 9 are absent from the latest source snapshot, and 220
  met a hotspot rule at some time that day. These are dated observed counts.
- Live reads found separate curves for 其他通信设备、光伏发电、低空经济、跨境支付.
  Desktop search, selecting 低空经济, and paging 13 selected curves across two
  groups passed. The final view was restored to automatic hotspot tracking.
- Source assets matched served SHA-256 values. No browser console errors were
  observed after reload. Legacy direction counts remained 39 and 48.
- Focused directory, gateway, legacy rotation, Collector, Web/UI and architecture
  tests passed, as did the Node curve/search/revision tests and diff whitespace check.
- An isolated copy of real snapshots measured about 14 seconds for cold full
  materialization and 8.4 seconds for a changed minute with warmed normalization;
  unchanged-source checks were about 0.5 seconds. Live manifest read was about
  0.23 seconds and a 12-curve read about 0.18 seconds on this machine.
- No whole-repository suite or market-hours provider smoke was run. The current
  day was non-trading; optional repair transport and budgets were fixture-tested.
  Live market-hours load, provider availability and subsequent-day gap reduction
  remain to be observed. Historical gaps and unverified market-wide coverage are
  intentionally visible rather than presented as repaired.

## Completed backfill and shared-chart acceptance (2026-09-13 23:44 Shanghai)

- Published revision `330d3660a0d66fe8d4c49c15212122b6bdaf0caa636fb903aa435fa123dbc929`
  contains 1,000 verified curves and 240,984 real points. Every curve covers all
  240 expected provider minutes through September 11 15:00. Some also retain a
  real 13:00 quote point; points are not invented to equalize lengths.
- The final resumed batch saved 785/785, failed 0, left 0 unattempted and confirmed
  required legacy targets unchanged. Earlier successful batches were retained.
  Exact audit and all-curve readback are in `.tradex-run/catalog-backfill-20260911.jsonl`
  and `.tradex-run/catalog-backfill-20260911-acceptance.json`.
- Quote/hotspot evidence still ends at 14:52; 9 source boards are absent from the
  last raw snapshot. Complete minute curves do not establish fresh quote coverage.
  Concurrent reviewed-pool maintenance now maps 88 boards and leaves 912 unclassified.
- Managed Collector PID 57192 and dashboard PID 53928 were restarted. Live readback
  reports Collector running with a current heartbeat; the original accepted-real
  snapshot revision remains `17db8906d49aeec435249e3515a62a3aa756f6d79b7bc2a8094e14310ce4d22f`.
- The directory now directly uses the original chart renderer. Browser checks
  verified all 12 paths have one start and the original smooth lunch bridge,
  12 endpoint names, real-point tooltip, click-to-focus and outside-click reset.
  Both sort directions and the full 1,000-entry/34-page scope were verified.
- Focused Python checks passed: 95 gateway/risk/catalog/architecture tests and
  39 original UI tests. Five Node checks passed for shared paths, gaps, full search,
  sorting and revision validation. Served assets matched local SHA-256 values;
  no browser errors were observed. Market-hours contention remains unverified.
