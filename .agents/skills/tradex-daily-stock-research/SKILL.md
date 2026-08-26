---
name: tradex-daily-stock-research
description: "Use for Tradex daily A-share stock selection: generate or read archived candidate pools, explain factor evidence and exclusions, review realized outcomes or walk-forward backtests, and maintain the daily-stock-selection feature. Use the existing provider-neutral Tushare gateway. Do not use for general multi-asset research, direct provider calls, or unsupported price predictions."
---

# Tradex Daily Stock Research

This skill adapts useful workflow ideas from an isolated third-party package without installing it or copying its collectors, scoring code, prompts, or runtime. Tradex remains the sole owner of acquisition, normalization, quality, selection, scheduling, persistence, and Web contracts.

## Choose the operating mode

### Read or generate a candidate archive

Read [references/runtime-contract.md](references/runtime-contract.md), then use `scripts/daily_selection_client.py`.

- Default to a read-only archive request.
- Use `--trade-date` when the user names a date.
- Use `--generate` only when the user explicitly asks to create or refresh the daily result. Generation writes one local immutable archive record through the Web service.
- If no archive exists, report that state. Do not silently generate one.

### Explain a selection or realized outcome

Consume the archived payload already returned by the service. Explain candidates from their stored reasons, risks, factor contributions, rank, and data-quality metadata. Explain outcomes from the archived next-session entry and realized return fields. Do not reacquire provider data merely to make the explanation look richer.

### Review a backtest or methodology

Use the versioned selector configuration and the existing `walk_forward_backtest` path. State the evaluation horizon, next-session entry rule, costs, coverage, benchmark, sample size, and whether the input snapshots are real or synthetic. Treat a single realized session as an observation, not proof of alpha.

### Change or diagnose the feature

Trace the implemented path before editing:

`Provider -> Mapper -> Canonical Contract -> Quality Gate -> Data Gateway -> Selection Service -> Web API`

Keep one owner for refresh, retry, cache, scheduling, and persistence. Preserve unrelated dirty-worktree changes. Read [references/adaptation-boundary.md](references/adaptation-boundary.md) whenever work would change acquisition, scoring, prediction language, source-package boundaries, or feature scope.

## Evidence rules

- A candidate ranking is not a buy list, probability forecast, target price, or return promise.
- Report `trade_date`, provider/as-of time, quality state, coverage, and important exclusions.
- Never manufacture missing values, backfill history from a current value, or treat a non-empty provider payload as semantic success.
- Keep financial factors point-in-time. A suspended or zero-turnover stock fails the liquidity gate.
- Base explanations on archived reasons, risks, contributions, and quality warnings; label any additional interpretation as inference.
- Derive outcomes from the next valid session open and the contract's configured horizon.

## Runtime boundary

- Use the local Web API or provider-neutral feature service. Dashboard, skill scripts, MCP, and lake code must never import provider implementations.
- Raw Tushare fields, units, timestamps, and error conventions terminate at the mapper boundary.
- Do not run the external package's Tencent/Eastmoney/Sina/AkShare collectors, search or sentiment tools, voice mode, global-asset workflows, prediction stacks, or council/persona orchestration.
- Do not create a second scheduler, cache, retry loop, archive, or configuration source inside this skill.
- Do not bypass the service's trade-calendar, 18:00 manual gate, 18:30 automatic schedule, quality checks, immutable archive, or structured errors.

## Response shape

Keep user-facing research compact and auditable:

1. Result state and trade date.
2. Data quality, coverage, provider, and as-of time.
3. Ranked candidates with stored evidence and risks.
4. Realized outcome or backtest evidence when available.
5. Explicit limitations and the next safe action.

## Validate changes

For skill-only edits, run the skill validator and exercise the client in read-only mode plus its invalid-input cases. For feature implementation changes, run the nearest affected daily-selection tests, the architecture boundary test for cross-layer imports, the JavaScript syntax check, and the dashboard contract test. Use a real Tushare smoke only when provider semantics, routing, quality, scheduling, or persistence changed; otherwise preserve the existing verified archive.
