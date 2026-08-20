# Project-level development constraints

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
