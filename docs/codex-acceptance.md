# Codex acceptance record

This implementation adds app task operation and Codex/mixed-provider background execution. The automated checks cover installation and runtime contracts. They do not prove that an arbitrary real project or current provider model will finish its work unattended.

| Area | Evidence |
|---|---|
| Installation | Claude/Codex/both matrix, paths with spaces and symlinked source, customized asset conflicts, managed instructions, constitution preservation, active integration ordering |
| Actual Spec Kit | Pinned 1.2.2 (`uv tool install specify-cli==1.2.2`): real Bureau dual-host installation, clean 0.7.5 upgrade and customized-helper refusals before and after manual manifest advancement live-verify the pristine render/content guard; measured results below. Recovered-file handling remains covered by fake-CLI regressions only |
| Current-task stages | Real Git fixture through spec, implementation, independent QA and review; only Linear traffic is replaced |
| Resume and coexistence | Concurrent claims, stale state/HEAD/config/branch rejection, held branches, unregistered checkouts, interrupted execution and idempotent completion |
| Provider adapters | Fake CLI processes with real argv/stdin, large prompts, structured final output, diagnostic separation, auth/quota/errors, timeout and cancellation; process-group cleanup also covers descendants surviving an exited CLI |
| Codex implementation | Actual shell loop, real Git commits and independent project tests with a fake Codex executable; test failures cannot advance state |
| App test action | Local venv dependency and HTTP endpoint; loopback test passed outside the restricted tool sandbox, without external network calls |
| Review target | Real Git and fake providers verify actual PR base selection, pinned head/base diffs, retarget/base advancement invalidation, legacy review-stop compatibility and rejection of stale approvals |
| Supervision | Bounded tick outcomes, actual Done distinction, pause, conservative schedule failures, actual shared workflow core, provider-specific quota signals and quiet monitor |
| Migration | Exact backup, preview, repeat application, preservation of IDs/models/extensions and effective legacy Codex selection across runner overrides; malformed/future-version rejection, real Bureau interface checks and enabled-stage executable requirements |
| Live provider smoke | Codex CLI 0.153.4 fixed a seeded addition bug in a disposable repository; independent Python tests passed and the fixture committed it. Claude Code 2.1.261 returned a valid structured APPROVE review from supplied code/test evidence with tools disabled |
| Platforms | CI runs the full suite on GitHub-hosted Ubuntu and macOS; macOS uses `/bin/bash` 3.2. The HTTP fixture is mandatory in CI |

## Spec Kit installer evidence

Operator-supplied local evidence dated 2026-10-21 (21.10.2026), recorded with `speckit-upgrade/e2e/live.sh` and `L-*.log`, covers four real Bureau installer runs with the pinned Spec Kit 1.2.2 CLI:

- **A — Empty repository** (`L-dual.log`), `speckit --target both --active-integration claude --apply`: exit 0, Codex and Claude registered, default Claude, 0 modified and 0 missing managed files.
- **B — Unmodified 0.7.5 copy of a real repository** (`L-clean.log`): exit 0, 0 modified and 0 missing managed files, constitution bytes unchanged.
- **C — Customized 0.7.5 helper** (`L-mod.log`), with one comment appended to `.specify/scripts/bash/common.sh`: refused with exit 1, tree unchanged.
- **D — The same customization after manual `specify integration upgrade claude`** (`L-manual.log`): the shared manifest advanced to 1.2.2 and kept the customization; Bureau still refused with exit 1, tree unchanged.

These runs live-verify the pristine helper render/content guard, including refusal after manual manifest advancement, and the init/install/use lifecycle. Earlier supplied evidence also covers real 1.2.2 helpers resolving customized 0.7.5 plan/tasks templates and a headless specify → plan → tasks run. They do not qualify the complete Bureau pipeline.

Targeted tests run the same render path with a fake `specify` executable, trace its working directory, model an advanced shared manifest retaining legacy hashes, and verify refusal before any repository-writing CLI call. They also verify recovered templates and skills survive forced initialization, and customized `.specify/.gitignore` bytes survive both successful and failed apply. Recovered-file handling is not live-qualified. This fix round uses targeted checks without network; it does not rerun the operator's live fixtures or the full suite.

For the remaining recovered-file live qualification, use disposable copies with recovered helpers, templates and skills. A helper marked recovered must cause exit 1 with every conflicting path named and no adopting-tree changes in preview or apply. Confirm recovered templates/skills retain their exact bytes after apply; intentional customizations may then appear modified in status. Do not run `specify integration upgrade` on a copy with modified helpers to prepare it: back up and resolve those helpers first.

## Live adoption and evidence boundaries

On 2026-09-07, a maintainer exercised a representative private adopting repository with existing instructions, legacy configuration, customized scripts and retained worktrees. Separate tickets covered:

- Current-app spec, implementation, real project tests, QA and review, ending with an APPROVE result while merge remained disabled.
- A bounded Codex background route with interruption, inspected recovery and resume, independent project tests, and an actual Codex review ending at the durable review stop (exit 20).
- A mixed-provider route with a terminated Codex implementation, preserved edits and ownership, inspected recovery, resumed implementation, independent project tests and an actual Claude review ending at the same durable stop.

The installed runtime fixture suite and project-specific regression checks passed. Independent project tests passed, and the product-code tree remained unchanged in the subsequent review/documentation updates. Existing user worktrees and customizations were preserved; no recurring dispatch was enabled. These checks supplement the isolated provider smoke, which used Codex CLI 0.153.4 and Claude Code 2.1.261.

The adopting repository, ticket records, provider logs and PR evidence are private. This paragraph is a maintainer-reported qualification summary, not publicly reproducible ticket evidence. Public source tests use fake provider/network boundaries as shown above. This work does not qualify arbitrary projects, app settings, recurring app automations, unattended scheduling, every provider/model combination, or a live automatic merge. Each adopting project still needs a bounded acceptance run:

1. Install both interfaces with existing user instructions and configuration; verify the active Spec Kit integration and all custom-asset resolutions.
2. Run an app ticket through specification, implementation, project tests, QA and review; inspect its PR and stop-before-merge result.
3. Interrupt and resume saved work, verifying ownership, branch identity and preserved artifacts before recovery.
4. Exercise the intended Codex and mixed-provider background routes, including authentication, project dependency/network permissions, PR evidence and Linear transitions.
5. Verify review-only execution and durable stops; enable recurring supervision only after qualification and only when requested.

Use the configured provider/model available to the account. Environment restrictions are recorded separately from failing project tests. CLI token/cost estimates cannot substitute for billed account usage.
