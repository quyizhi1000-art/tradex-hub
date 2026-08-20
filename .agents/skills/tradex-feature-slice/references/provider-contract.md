# Provider-contract checklist

Read this reference only when a change adds or modifies a market-data contract, provider mapping, routing attempt, or paid-source integration.

## Canonical contract

- Name the contract by capability and version, for example `market_breadth.v1` or `sector_quote.v1`.
- Use strict immutable models. Reject unknown fields at the canonical boundary rather than silently leaking provider schema.
- Use canonical instrument identifiers such as `000001.SZ`; keep provider codes as explicitly named metadata only when consumers genuinely need them.
- Express percentages in percentage points, volume in shares, monetary amounts in CNY, and datetimes with timezone information. Record different semantics explicitly instead of guessing a conversion.
- Include provider, provider request id when available, provider timestamp, fetch timestamp, quality status, and stable quality flags.

## Quality and fallback

- `accepted`: all required semantics and units are verified.
- `degraded`: the result remains safe to display but optional coverage, provider timestamp, or verified metadata is missing. Preserve explicit `None` values and quality flags.
- `rejected`: required semantics, invariants, units, identifiers, or timestamps are invalid. A rejected provider attempt must be eligible for fallback unless the capability is explicitly exclusive.
- `unavailable`: no provider produced a safe canonical result. Return an explicit unavailable shape or error defined by the feature contract; never fabricate zeroes.

Validate before recording provider success. Raw HTTP success, a non-empty frame, or a provider's own success flag is insufficient.

## Provider conformance fixtures

For every provider participating in the capability, cover the applicable cases without live network calls:

1. representative accepted payload;
2. missing optional fields producing degraded quality;
3. invalid required field, unit, timestamp, or identifier producing rejection;
4. provider-specific aliases mapping to identical canonical fields;
5. fallback from a rejected primary attempt to a valid secondary attempt;
6. stable legacy/Web/MCP serialization after switching providers.

Keep secrets, API keys, quotas, and billing behavior outside fixtures. Live paid-source smoke tests require explicit authorization and must be bounded to the minimum call count.
