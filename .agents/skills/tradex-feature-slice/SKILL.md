---
name: tradex-feature-slice
description: Plan or implement Tradex desktop-web market-data features as provider-neutral vertical slices. Use when a change adds or alters market-data semantics, a refreshable metric, source routing, freshness/cache/fallback behavior, a canonical contract, a Web API payload, MCP exposure, or a data-lake feed. Do not use for UI-only layout, styling, copy, client-side interaction, or presentation changes that consume an existing stable API, or for unrelated operational work.
---

# Tradex Feature Slice

Keep the user-visible capability stable when providers, prices, or routing priorities change. Treat Web, MCP, and data-lake delivery as adapters around one canonical feature result, not as independent implementations.

## Establish the slice

Inspect only the layers affected by the requested behavior. For a data-capability change, inspect the affected consumer, gateway contract, provider registrations, refresh path, and nearest tests. State or record:

- consumer behavior and required fields;
- field semantics, units, timezone, ordering, and stable identifiers;
- freshness target, TTL, allowed stale window, and trading-session behavior;
- accepted, degraded, rejected, unavailable, and fallback behavior;
- expected call rate, paid-source cost exposure, and request cardinality;
- compatibility obligations for existing Web or MCP payloads.

Ask only about ambiguity that materially changes observable behavior or cost. Preserve the user's approved architecture and avoid broad rewrites that are not required by the feature.

If a consumer-only change preserves the existing business contract, data semantics, routing, and refresh/cache ownership, keep the work in the consumer layer. Do not expand it into provider, gateway, MCP, or data-lake changes. UI code may own presentation state, client-side interaction, layout, visualization, and sorting or filtering of already delivered data when those behaviors do not redefine business semantics.

When the slice adds or changes a provider or canonical market-data contract, read [references/provider-contract.md](references/provider-contract.md) before designing or editing it.

## Place responsibilities

Follow this dependency direction for new work:

1. `tradex.data_sources`: transport, authentication, throttling, and raw provider acquisition.
2. `tradex.data_gateway.providers`: provider-specific mapping and unit conversion.
3. `tradex.data_gateway.contracts` and `quality`: strict versioned models and quality decisions.
4. Gateway or provider-neutral feature service: orchestration, fallback-aware result construction, refresh, single-flight, and the sole feature cache.
5. Dashboard, MCP tools, and data-lake adapters: thin serialization and compatibility mapping only.

Do not add direct `tradex.data_sources` imports to dashboard, MCP tool, or data-lake code. Do not add provider implementation imports to `tradex.data_gateway`. Keep provider names and raw field aliases out of consumer logic.

If normalization or quality validation happens after `SmartRouter.route()` returns, do not describe the raw fetch as a valid provider success. Either move the validation into the routed attempt or introduce the smallest provider-neutral seam that lets an invalid payload fall back. Do not catch a downstream contract failure and return it as accepted data.

## Implement and verify

- Add contract or behavior tests before changing the implementation when practical; demonstrate that a new regression test fails for the intended reason.
- Use provider fixtures for mapping tests. Do not call paid or live sources in ordinary unit tests.
- Cover the primary provider, at least one fallback shape, partial data, invalid units or timestamps, and backward-compatible serialization when they apply.
- Run only the nearest affected tests plus `pytest tests/test_architecture_boundaries.py` for cross-layer changes.
- Report the source of truth for caching and refresh, checks actually run, skipped live checks, and any remaining baseline architecture debt.

Do not install a workflow framework, create additional agents, split processes, or change deployment topology merely because this skill applies.
