# Runtime contract

## Authoritative path

The daily-selection data path is:

`Tushare client -> provider fetcher/mapper -> daily stock snapshot v1 -> deterministic selection engine -> service -> immutable archive -> desktop Web dashboard`

Use these implementation owners when diagnosis or maintenance requires source inspection:

- Tushare provider-frame acquisition: `tradex/src/tradex/data_sources/tushare_fetchers.py`
- Provider normalization: `tradex/src/tradex/data_gateway/providers/stock_selection.py`
- Provider-neutral routing and quality gate: `tradex/src/tradex/data_gateway/stock_selection.py`
- Canonical input contract: `tradex/src/tradex/data_gateway/stock_selection_contracts.py`
- Selection and outcome contracts: `tradex/src/tradex/stock_selection/contracts.py`
- Deterministic ranking and backtest: `tradex/src/tradex/stock_selection/engine.py` and `backtest.py`
- Scheduling, retry, and archive ownership: `tradex/src/tradex/stock_selection/service.py` and `store.py`
- Desktop API adapter: `tradex/src/tradex/dashboard/__main__.py`

Search by contract names if repository paths move. Do not replace this path with direct provider calls from the skill.

## Local Web API

### Read archived results

`GET /api/daily-stock-selection/history?trade_date=YYYY-MM-DD&limit=90`

- `trade_date` is optional.
- `limit` must be from 1 through 365.
- Success returns `daily_stock_selection_archive.v1` with schema version `1`.
- The request is read-only and must not trigger acquisition.

### Generate the current result

`POST /api/daily-stock-selection`

- Call only after explicit user intent to generate or refresh.
- The request starts or reuses the service-owned single background job and returns `daily_stock_selection_generation.v1` immediately. A running job uses HTTP `202`; an already archived result uses HTTP `200` with `state=succeeded`.
- The service, not this skill, decides the eligible trade date, time gate, retries, quality acceptance, and archive state.

### Read generation status

`GET /api/daily-stock-selection/generation`

- This request is read-only and never starts acquisition.
- `state` is `idle`, `running`, `succeeded`, or `failed`; `phase` exposes `queued`, `acquiring`, `selecting`, `archiving`, `completed`, or `failed` as applicable.
- A succeeded job embeds the existing `daily_stock_selection_result.v1`; a failed job returns the safe error, the failed phase, and a non-secret failure type.
- Manual and automatic callers observe and reuse the same running job.

### Error meanings

- `400`: invalid client input.
- `409`: domain state prevents a job from starting, such as time-gate or calendar conditions.
- A background acquisition or quality failure is reported as `state=failed` with the service's safe error; it is not converted into a successful archive.
- `502`: the local service could not start the job or read its state.

Preserve the service's structured message and do not retry a deterministic error without a changed input or external state.

## Scheduling and persistence

- Manual generation opens at 18:00 Asia/Shanghai.
- Automatic generation starts at 18:30 Asia/Shanghai.
- Automatic acquisition has three bounded attempts, spaced ten minutes apart.
- The default archive is `%USERPROFILE%\.tradex\daily_stock_selection.sqlite3`; an existing project environment override may change it.
- A `(trade_date, config_version)` result is immutable. Repeated requests return the existing record rather than silently replacing canonical evidence.

## Selection semantics

- Default selector configuration: `daily-stock-selection-balanced.v2`.
- The implementation owns exact thresholds, weights, and tie-breakers; do not duplicate them in this skill.
- Hard gates cover listing age, trading state, liquidity, and required point-in-time evidence.
- Ranked evidence may include valuation, dividend, profitability, leverage, growth, and momentum factors.
- Robust normalization uses industry-aware statistics where available and a defined missing-data penalty.
- Ranking order is deterministic for the same snapshot and configuration.

### Pattern-screen attachment

- Current-version archives attach `pattern_screens` to `daily_stock_selection.v1`.
- `long-upper-shadow-main-board.v1` evaluates the signal day and previous 14 completed trading sessions.
- One event requires upper shadow at least 3 percent of close, at least twice the real body, and at least 50 percent of the daily high-low range.
- A match requires at least two events, canonical market `主板`, a non-ST/non-retiring name, and all 15 verified active-session OHLC bars.
- Each candidate preserves every matching trade date and calculated ratio. Missing bars exclude that instrument instead of being imputed.
- The result is a candlestick proxy only. It must not be described as proof of participant intent or a return forecast.

## Outcome and backtest semantics

- Entry uses the next valid trading session open defined by the contract.
- Suspended entry sessions are skipped rather than imputed.
- A result is evaluable only when outcome coverage reaches the contract threshold; current default is 70 percent.
- Backtests disclose transaction costs, benchmark comparison, sample size, horizon, and input snapshot provenance.
- CSI 300 is the default benchmark when present in the canonical input.
- Every historical evaluation must use the point-in-time snapshot for that date. Never reconstruct old factors from today's fundamentals.

## Client examples

Run from the project root:

```powershell
G:\money\.venv\Scripts\python.exe .agents\skills\tradex-daily-stock-research\scripts\daily_selection_client.py
G:\money\.venv\Scripts\python.exe .agents\skills\tradex-daily-stock-research\scripts\daily_selection_client.py --trade-date 2026-08-25
G:\money\.venv\Scripts\python.exe .agents\skills\tradex-daily-stock-research\scripts\daily_selection_client.py --generate
```

The final command performs a local write and must be used only with explicit user authorization.
