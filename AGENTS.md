# Project-level development constraints

## Lean path for small tasks

- Treat a task as small when it is one local, reversible concern with narrow acceptance criteria and it does not cross architecture, schema, authentication, security, licensing, deployment, external-write, or release boundaries.
- For a small task, do not spawn subagents and do not invoke a requirement or plan audit unless the user explicitly asks for either one.
- Batch the necessary reads into one pass, make one coherent edit batch, run the single nearest focused test or validator, and stop as soon as the stated acceptance criteria pass.
- Do not add a full suite, build, browser run, online check, repeated passing test, or speculative follow-up audit to a small task. A failed focused check may be rerun only after its concrete cause has changed.
- If the initial read shows that the task crosses one of the excluded boundaries, leave the small-task path and state the concrete reason before expanding execution.

## Regression safety and causal verification

- Treat preservation of relevant existing correct behavior as part of every fix or optimization's acceptance criteria, not as an optional follow-up. Before editing, identify the exact requested change, the real entrypoint and runtime owner, the affected callers or consumers, and the closest behaviors that must remain unchanged.
- Establish causality before changing code whenever the issue can be reproduced safely. Use the smallest deterministic test, probe, log, API readback, or visible-state check that distinguishes a regression caused by the current code from a pre-existing defect, stale process, cache, browser asset, or changed data state. Do not roll back or modify code based only on temporal correlation.
- Make the narrowest coherent diff that satisfies the request. Do not combine it with unrelated refactors, renames, formatting, cleanup, dependency changes, or silent changes to defaults, fallback behavior, error handling, timestamps, units, freshness, or data semantics.
- For changes to shared functions, contracts, refresh/cache/single-flight ownership, persisted state, or interface payloads, inspect the relevant callers before editing. If that inspection reveals a cross-boundary impact, leave the small-task path and state the concrete expanded risk before proceeding.
- When meaningful, add or update a regression test that proves the requested scenario and the nearest protected sibling behavior. If a useful automated test is not feasible, state why and use the closest deterministic evidence instead; never present an untested assumption as proof.
- After one coherent edit batch, run the nearest affected test or domain suite once. For runtime or user-visible defects, also use the cheapest direct readback that proves the active entrypoint, served asset, API, or managed process actually loaded the change; add a browser run only when the affected interaction or layout cannot be verified more cheaply. Source inspection or local test success alone is not live-runtime proof.
- Before handoff, inspect the final diff for unintended deletions, broadened conditions, changed defaults, and edits outside the declared scope. Report the files changed, protected behaviors checked, validations passed or failed, layers intentionally not verified, and what remains for CI. Do not promise zero regressions or describe an unverified layer as working.
- If the requested optimization conflicts with an existing correct behavior or business invariant, challenge the request with concrete evidence before changing that behavior. Do not silently choose one side of the conflict.

## Tradex MCP tool profiles

- The default `tradex` MCP entry exposes only the eight core discovery and market-read tools in `.codex/config.toml`.
- `tradex_ops`, `tradex_market`, `tradex_research`, `tradex_provider`, and `tradex_lake` are disabled capability profiles with non-overlapping allowlists. Enable only the profile needed by a new task; never preload every profile merely for discovery.
- Keep `MCP_DOCKER` disabled in this project. It is the LMGameDev Docker gateway and is not a Tradex dependency.
- MCP configuration is frozen when a task starts. After changing an enabled profile, start a new task so the tool snapshot is rebuilt.

## Temporary mobile exclusion

- Until the project owner explicitly revokes this section, treat desktop web pages and desktop user experience as the only UI target for the project.
- Do not propose, design, implement, optimize, or test mobile pages or mobile-specific user experience. This includes mobile breakpoints, mobile-only layouts or navigation, touch-specific interaction work, and mobile-device compatibility work.
- Do not add mobile requirements, acceptance criteria, test cases, or delivery scope to plans, issues, or implementation work.
- If a later request would require mobile-specific work, identify the conflict and wait for the project owner to explicitly revoke or amend this section before proceeding with that mobile scope.
- This restriction has no automatic expiry. Only an explicit instruction from the project owner to revoke or amend it ends or changes the restriction.
- Existing mobile or generally responsive behavior is not a current development target, but do not deliberately remove or break it unless the project owner separately authorizes that change.

## Tradex architecture guardrails

- Build new or changed desktop-web market-data features around a provider-neutral, versioned business contract. Provider field names, units, timestamps, and error conventions must be normalized before they reach dashboard, MCP tool, or data-lake code.
- Dashboard, MCP tools, and data-lake adapters must not add direct imports from `tradex.data_sources`. Existing violations are tracked by `tests/architecture_boundary_baseline.json`; remove them incrementally and never expand that baseline without explicit project-owner approval.
- `tradex.data_gateway` may use only the public `tradex.data_sources` router facade. It must not add imports from provider implementation modules such as `akshare_fetchers`, `fuyao_fetchers`, or other source-specific modules.
- Data-lake code must not add dependencies on dashboard modules. Shared computation belongs in a provider-neutral feature service or gateway module consumed by both interfaces.
- Assign one owner for refresh, stale-data, single-flight, and cache policy for each feature. Dashboard and MCP adapters must not create competing caches for the same canonical result.
- Run the focused architecture boundary test for cross-layer changes. Removing a recorded violation requires deleting its baseline entry in the same change.
