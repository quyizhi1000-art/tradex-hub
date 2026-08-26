# Adaptation boundary

## Source reviewed

An isolated copy of SkillHub slug `aistockresearcher` was audited on 2026-08-26. Its package metadata reported version `1.0.41`; its internal skill document reported version `9.5.0`.

No visible license declaration was found in the audited package. Therefore this Tradex skill must not copy, redistribute, import, execute, or depend on that package's implementation. This project skill is an independent adaptation of general workflow ideas only.

## Ideas retained

- Probe the actual environment and available data before claiming a result.
- Degrade honestly when required evidence is missing.
- Lead with structured, inspectable evidence rather than narrative certainty.
- Prefer walk-forward evaluation over in-sample anecdotes.
- Bound external work and retries.

## Tradex replacements

| External package concept | Tradex implementation boundary |
| --- | --- |
| Direct Tencent, Eastmoney, Sina, or AkShare collectors | Existing provider-neutral Tushare gateway and mapper |
| Global multi-asset workflows | Desktop A-share daily-selection contract |
| Multiple predictor and scoring stacks | Canonical snapshot v1 plus deterministic selector configuration |
| Package-local caches and data files | Selection service and immutable archive store |
| Free-form command workflow | Versioned Web API contracts and the local client script |
| Probability, target, and forecast language | Factor score, rank evidence, risks, and realized outcomes |

## Explicitly excluded

- Voice interaction.
- External search, news, sentiment, and social-media collection.
- Global equities, futures, crypto, options, and portfolio execution.
- DCF, Monte Carlo, Kelly, VaR, price-target, and price-probability claims.
- Multi-persona or master-council orchestration.
- Any direct provider call from dashboard, MCP, lake, or skill code.

Adding one of these later is a separate product and data-governance decision, not a small extension of this skill.
