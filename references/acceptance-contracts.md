# Acceptance contracts, version 1

This offline format declares intended checks before implementation. Enforcement is not active. No stage reads the contract or calls the validator; there is no enablement key. Freezing, verifier execution, receipts and human approval are not implemented.

A well formed contract proves only that its declaration is well formed. It does not prove requirements are complete, that any verifier exists or passed, or that the product is accepted. A `human` entry reserves a decision; it records no answer.

## Files and command

Keep an optional `acceptance.json` beside a feature's `spec.md` and `tasks.md`. Invoke the script explicitly, supplying all three inputs:

```sh
python3 scripts/bureau-acceptance-contract.py validate acceptance.json --spec spec.md --tasks tasks.md
```

The template is installed by the existing generic scripts scope. No installer behavior changes. Existing installations need no action unless they want the offline tool; compare local script drift before resyncing that scope.

The CLI writes exactly one JSON object to stdout, including on errors. Exit codes are 0 for a well formed declaration, 22 for a malformed contract or broken reference, and 2 for usage errors or unreadable inputs. `well_formed` describes only the declaration; `enforcement_active` is always false. `scope` states the limits above. `errors` contains `location` and `message` pairs sorted by location then message, lexicographically. Locations use JSON Pointer escaping (`~0`, `~1`); the empty pointer identifies the contract root. `/spec/lines/N` and `/tasks/lines/N` identify one-based source lines; `/spec`, `/tasks` and `/cli` identify input or usage problems. Unreadable input takes precedence over exit 22.

## Closed schema

All fields listed below are required except the mutually exclusive `checks` and `mode`. Unknown fields and duplicate JSON keys are errors at every level. There are no command fields. Identifiers are case sensitive.

| Object | Fields |
| --- | --- |
| Contract | `schema_version`: integer 1; `contract_id`: opaque nonblank string; `revision`: integer at least 1; `retired_requirement_ids`: array of requirement IDs; `requirements`: array of requirement objects |
| Requirement | `requirement_id`: stable Spec Kit ID; `spec_ref`: that same ID declared in the spec; `task_ids`: array of declared task IDs; exactly one of a nonempty `checks` array or `mode: "human"` |
| Check | `verifier_id`: opaque nonblank string; `expect`: one of the expectation objects below |

Reuse the spec's `FR-` or `SC-` ID with at least three digits. Preserve it through rewording and reordering. Retire replaced IDs instead of recycling them. Retired IDs can remain after their spec declarations are removed, but cannot also be used by a requirement object. Every currently declared spec ID must be mapped or retired. Duplicate requirement IDs and duplicate entries in ID arrays are errors. Empty requirements, retired-ID and task-ID arrays are allowed; completeness still needs human review.

| Intended verifier | Exact `expect` shape |
| --- | --- |
| Test | `{"kind":"pass"}` |
| Artifact presence | `{"kind":"present","path":"deliverables/result.txt"}` |
| Nonempty artifact | `{"kind":"nonempty","path":"deliverables/result.txt"}` |
| Artifact digest | `{"kind":"digest","path":"deliverables/result.txt","sha256":"<64 hex digits>"}` |
| Regression | `{"kind":"fails_on_base_passes_on_candidate","base":"<40 hex digits>","fixture":{"commit":"<40 hex digits>","sha256":"<64 hex digits>"}}` |

Expectation kinds describe intended semantics only. The validator neither resolves verifier IDs nor inspects artifacts, commits or fixture bundles. Hex digits may use either case. Artifact paths must be nonblank relative paths with a file component, without absolute or drive-qualified paths, `..` components, backslashes or control characters. Checks with identical contents (ignoring JSON object key order) are duplicates within a requirement. Different expectations may name the same verifier; compatibility with a future registry is unassessed.

## Source declarations and limits

The named parser constants use the Spec Kit declaration formats exactly:

```text
SPEC_DECLARATION: ^\s*[-*]\s+\*\*((?:FR|SC)-[0-9]{3,})\*\*\s*:
TASK_DECLARATION: ^\s*-\s+\[[ xX]\]\s+(T[0-9]{3,})\b
```

For example, `- **FR-001**: System MUST ...`, `- **SC-001**: ...`, `- [ ] T001 Create ...` and `- [x] T003 [P] Configure ...` declare IDs. Every matching line counts, including inside a code block, so a Markdown quirk cannot hide a requirement. To show an example without declaring it, write it in a form that does not match these patterns. Duplicate declarations are errors. Prose mentions and other list formats declare nothing.

The contract may contain at most 256 KiB (UTF-8 bytes, checked before parsing), 128 requirements and 256 checks across all requirements. Booleans are not integers. Nonstandard JSON constants and malformed hashes are rejected. All inputs must be regular UTF-8 files; an input that is itself a symlink is refused. Directories above it may be symlinks, as `/tmp` is on macOS.

## Runnable examples

[Complete contract](acceptance-contract-examples/complete.json) covers all five expectation kinds. [Human-reserved contract](acceptance-contract-examples/human.json) reserves both decisions for people. Each validates against the supplied synthetic [spec](acceptance-contract-examples/spec.md) and [tasks](acceptance-contract-examples/tasks.md):

```sh
python3 templates/scripts/bureau-acceptance-contract.py validate references/acceptance-contract-examples/complete.json --spec references/acceptance-contract-examples/spec.md --tasks references/acceptance-contract-examples/tasks.md
python3 templates/scripts/bureau-acceptance-contract.py validate references/acceptance-contract-examples/human.json --spec references/acceptance-contract-examples/spec.md --tasks references/acceptance-contract-examples/tasks.md
```

The IDs and hashes are synthetic. Neither example records executed checks or human acceptance.
