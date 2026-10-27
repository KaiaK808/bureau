# 0009 Literal environment filtering and validated shell arithmetic

- **Status**: proposed (v3.5, PR #76)
- **Date**: 2026-10-26
- **Decided by**: Kai Ebert
- **Sources**: `2522557` (consumer-side numeric guards and filter hardening); `547669c` (actual exported environment rather than caller-local lookups); `f4a9826` (complete fail-closed snapshot transport); `8d34947` (conservative encoding identity); `docs/release-notes-v3.4.0.md:17` (released untrusted-child boundary); `docs/configuration.md:185` (numeric contract); `docs/configuration.md:197` (snapshot and trusted interpreter); `docs/configuration.md:201` (encoding policy); `templates/scripts/bureau-env.sh:164` (numeric guard); `templates/scripts/bureau-env.sh:451` (snapshot builder).

## Context

Branch-controlled tests, hooks and providers must not inherit Bureau's service credentials or shell startup hooks. The released filters established that boundary, but shell arithmetic can evaluate hostile input, and caller-local bindings can hide actual exported secret carriers. This ADR's proposed status covers the v3.5 hardening, not a claim that filtering first appeared in v3.5.

## Options considered

- Pass the inherited environment unchanged, retaining maximal tool compatibility while exposing credentials and executable startup settings.
- Filter using shell indirect lookups and numeric comparisons, preserving familiar code but permitting name evaluation, caller shadowing and arithmetic expressions.
- Inspect a literal exported snapshot with trusted tooling and validate numeric text before arithmetic, adding overhead and conservative removals to close those paths.

## Decision

Keep the released default/clean child-environment boundary and read actual exported names/values through a NUL-framed snapshot, not enclosing function locals. Use an isolated interpreter resolved from the system default PATH; validate the complete transport before running the child and fail closed with 24 on missing trusted tooling or malformed/incomplete frames. Sources: `docs/release-notes-v3.4.0.md:17`; `547669c`; `f4a9826`; `docs/configuration.md:197`; `templates/scripts/bureau-env.sh:451`.

Remove credentials and their carriers, startup hooks and reserved filter names literally. Apply the six-character embedded-secret threshold only when Bash/helper encoding identity is proven; otherwise remove every nonempty secret wherever it appears in a value. Validate selected numeric settings as bounded ASCII digits before shell arithmetic, normalize leading zeros as decimal and warn with the key rather than rejected content. Sources: `8d34947`; `docs/configuration.md:201`; `2522557`; `docs/configuration.md:185`; `templates/scripts/bureau-env.sh:164`.

## Rationale

Environment text is data, including malformed names and expression-like numbers. Reading the exported snapshot closes caller-shadowing gaps, while conservative encoding handling avoids a character-count coincidence authorizing leakage. Consumer-side guards cover values that bypass the .env reader.

## Consequences

System-path Python 3.9+ and env -0 become requirements for the shell snapshot filter; a Python found only on a custom shell PATH is insufficient. Conservative removal can break an otherwise allowed variable containing a short secret. Each wrapped call pays snapshot/interpreter overhead. Resync all scripts coherently and restart long-lived drivers; .env precedence and the separate merge-gate numeric reader remain as documented.

## Known limits

Filtering is not an operating-system sandbox and cannot conceal files readable by the same account. Under proven equal encodings, short secrets embedded in larger allowed values retain the documented threshold behavior. Provider login/network variables still need to pass the selected clean policy. Local fixture and negative-control qualification does not establish live installation acceptance or a clean full-suite run in every environment.

## Reversal condition

Reconsider when a new carrier or name-evaluation bypass is reproduced, supported hosts cannot supply trusted snapshot tooling, or measured overhead is unacceptable. Replace the mechanism only with equivalent literal-data and fail-closed properties.

## Evidence

- `tests/test_env_arith_hardening.sh` exercises numeric consumers and expression-like input with harmless sentinels.
- `tests/test_env_shadowed_callers.sh` exercises exported carriers hidden by caller-local bindings and real stage calls.
- `tests/test_env_snapshot_fail_closed.sh` checks producer, framing and transport failures before child execution.
- `tests/test_env_snapshot_trust_locale.sh` checks interpreter trust, locale identities and conservative collision handling.
- `tests/test_untrusted_env.sh` and `tests/test_provider_untrusted_env.sh` check the shared shell/provider environment boundary.

## Open points

No current code/documentation disagreement was found within these specific guards. Earlier commit descriptions mention interpreter override behavior that docs/configuration.md:205 explicitly excludes at this snapshot; use the current implementation and documentation rather than copying that intermediate description.
