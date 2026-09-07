## Bureau workflow

Configuration: `.bureau.json`. Read its team, state, label, and repository settings before acting on a ticket. Pipeline templates and scripts are the source of truth for stage behavior.

Spec Kit skills are in `{{SKILLS_DIR}}/speckit-*/SKILL.md`. Use `{{INVOKE}}speckit-specify`, `{{INVOKE}}speckit-plan`, and `{{INVOKE}}speckit-tasks` for the specification flow. Preserve `.specify/memory/constitution.md` and project decisions. Bureau commands include `{{INVOKE}}linear-to-spec`, `{{INVOKE}}linear-implement`, and `{{INVOKE}}bureau-learnings`.

Resolve Spec Kit dotted hook names such as `speckit.git.feature` to the installed native `speckit-git-feature/SKILL.md` under the host's skills directory. Read arguments from the current request; a literal `$ARGUMENTS` placeholder does not mean the request is empty. Follow enabled mandatory hooks while preserving an already prepared run and its canonical branch.

On `codex/*` or detached checkouts, resolve the issue's exact approved feature directory before calling Spec Kit helpers. When helpers require numeric feature names, pass both `SPECIFY_FEATURE=NNN-approved-feature` and `SPECIFY_FEATURE_DIRECTORY=specs/NNN-approved-feature` for that invocation, using actual configured paths. Retained helpers may ignore feature.json; newer helpers may prefer stale feature.json over SPECIFY_FEATURE alone. Do not guess from duplicate prefixes, change the issue branch or overwrite another feature's plan.

Resolve issue branches from the `<!-- bureau-branch: ... -->` comment marker. Work on the issue's feature branch, reference the issue in commits, and preserve existing user changes. Background queue scripts operate on disposable worker worktrees; do not run their reset/clean operations on the current app task's checkout.

Follow the requested stage boundary. A request for a spec or review does not authorize implementation or merging. Existing background review can merge inline when the separate merge agent is disabled; do not use that path for a review-only request.

`logs/events.jsonl` contains pipeline events. `{{INVOKE}}bureau-learnings` drafts `LESSONS.proposed.md` for review; existing curated lessons remain advisory and must be preserved.

In Codex, use `$bureau` for current-task stages, status and resume. It uses the shared local ownership protocol and preserves the app checkout.
