# Tradex engineering-governance decision

Date: 2026-08-20

## Decision

Keep the approved modular-monolith and shared-data-runtime direction. Do not install a complete external spec-driven or multi-agent framework now.

Adopt three local controls instead:

1. `tradex-feature-slice` for provider-neutral vertical feature delivery;
2. `tradex-architecture-review` for evidence-based boundary review;
3. an executable import-boundary baseline that prevents new cross-layer debt without requiring a big-bang cleanup.

The generic `requirement-plan-audit` remains responsible for plan and approval discipline. The Tradex skills add domain boundaries that the generic workflow cannot know.

## External workflow audit

The audit inspected upstream source at fixed revisions; no package, plugin, hook, or skill was installed.

| Candidate | Inspected revision | Useful ideas | Adoption risk | Decision |
|---|---|---|---|---|
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | `1ebddd17f40dde15dfd28289e4493c3cf05ee9df` | Brownfield change artifacts; explicit proposal/design/spec/tasks boundary; verification pass | Requires its CLI; skills authorize planning-artifact writes; generated project state and CLI update lifecycle; telemetry is enabled by default unless disabled | Borrow artifact discipline only |
| [Superpowers](https://github.com/obra/superpowers) | `b36e0829c6d0140e93cfef2ca599b1b07d4a7797` | Root-cause debugging; evidence before completion; focused review | Session-start hooks; globally mandatory skill invocation; dogmatic delete-and-rewrite TDD; automatic worktrees/subagents; optional local visual server and telemetry surface | Borrow debugging and verification principles only |
| [GitHub Spec Kit](https://github.com/github/spec-kit) | `ad057b586f654836e8c4aef7ec20b8dd45873dee` | Constitution, spec/plan/task separation, consistency analysis | Larger Python/PowerShell/Bash CLI surface; branch and artifact generation; extensions can register automatically executed hooks; disproportionate ceremony for routine Tradex changes | Reconsider only for a major governed refactor |

Popularity was not treated as a selection criterion. The deciding factors were brownfield fit, auditable instructions, repository mutation surface, hook and network behavior, workflow overhead, and ability to encode Tradex-specific invariants.

## Boundary model

New work should move in this direction:

```text
provider transport/auth
        |
        v
provider-specific mapping and unit normalization
        |
        v
versioned canonical contract + quality decision
        |
        v
feature orchestration + one refresh/cache owner
        |
        +--------+---------+
        v        v         v
      Web       MCP     Data lake
```

The baseline test is intentionally not proof that the current architecture is clean. It records current forbidden imports and fails on both additions and stale allowances. Existing violations should be removed as affected features migrate; unrelated cleanup is not required for each change.

## Re-evaluation trigger

Re-evaluate OpenSpec or Spec Kit only after at least two substantial cross-boundary changes have used the local controls and one of these conditions is demonstrated:

- decisions still disappear between conversations;
- requirements and implementation repeatedly diverge despite local skills;
- multiple contributors need durable concurrent change artifacts;
- architecture reviews cannot trace requirement-to-test coverage economically.

Use subagents selectively for independent, read-heavy exploration, test analysis, or architecture review. Do not make multi-agent execution the default for routine implementation.
