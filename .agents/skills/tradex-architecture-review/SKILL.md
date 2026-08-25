---
name: tradex-architecture-review
description: Review Tradex changes for business-boundary, provider-neutrality, refresh/cache ownership, and interface-layer violations. Use for architecture reviews, significant refactors, paid-source integrations, or changes spanning dashboard, MCP, gateway, providers, or data lake. Do not use as a mandatory ceremony for UI-only layout, styling, copy, client-side interaction, or presentation changes that preserve existing contracts and runtime ownership.
---

# Tradex Architecture Review

Review the repository evidence, not the conversation's intended design. A good plan does not compensate for code that crosses the boundary.

When the user asks only for review or diagnosis, remain read-only. Do not implement fixes, update the baseline, or create external actions without separate authorization.

When a Dashboard change preserves existing contracts, data semantics, routing, refresh/cache ownership, and layer imports, treat it as UI-only. Do not expand it into an end-to-end architecture review.

## Review procedure

1. Establish the reviewed scope from the user request and current diff. Preserve unrelated dirty-worktree changes.
2. Identify the affected business capabilities and trace each path from provider acquisition through canonical contract and feature orchestration to Dashboard, MCP, and data lake.
3. Run `pytest tests/test_architecture_boundaries.py` when cross-layer Python imports changed. Read `tests/architecture_boundary_baseline.json` as debt, never as an approved pattern.
4. Check the boundaries and runtime ownership below.
5. Report findings by severity with file and line evidence, observable consequence, and the smallest credible correction. Distinguish facts from inferences.

## Required boundaries

- Dashboard, MCP tools, and data-lake modules do not acquire providers or call the router directly in new code.
- Gateway code may use the public router facade but does not import concrete provider fetcher modules.
- Provider-specific aliases, units, timestamps, identifiers, and error formats terminate in provider mapping code.
- Versioned canonical contracts and quality validation execute before data is accepted for consumers.
- Data lake does not depend on dashboard modules; both consume provider-neutral services.
- MCP and Web adapters serialize results but do not own routing, retry, fallback, business calculation, or duplicate caches.
- UI code may own presentation state, client-side interaction, layout, visualization, and sorting or filtering of already delivered data when those behaviors do not redefine business semantics.
- One runtime owner defines TTL, stale-while-revalidate, single-flight, and trading-session refresh behavior for a feature.
- A provider switch does not require consumer-field or page changes.

## Baseline policy

`tests/architecture_boundary_baseline.json` records current violations so the gate can be adopted without a big-bang rewrite.

- Added entries are regressions. Do not approve them merely by editing the JSON.
- Removed violations require removing their entries in the same change; stale allowances can hide reintroduction.
- Expanding the baseline requires explicit project-owner approval and a written rationale.
- Do not demand cleanup of unrelated baseline debt as a condition for a focused change.

## Challenge unnecessary architecture

Reject decomposition that adds a process, service, queue, database, framework, or Agent role without a measured isolation, scaling, reliability, or ownership need. Prefer a modular monolith and one shared data runtime until evidence requires a deployment split.

Do not recommend splitting MCP by provider. Recommend capability-based catalogs or profiles only at the interface boundary, while provider routing and canonical contracts remain shared.

## Completion

Summarize:

- blocking boundary violations;
- accepted existing debt versus new debt;
- refresh/cost consequences;
- tests and evidence reviewed;
- the smallest next architectural step.

Do not claim the architecture is clean merely because the baseline test passes.
