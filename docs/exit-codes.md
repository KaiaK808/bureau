# Exit codes & alerts

Every pipeline script exits with a code that classifies its outcome. `queue-loop.sh` reads the code, maps it to an alert class, and (optionally) fires a Telegram message — throttled so a stuck issue doesn't spam you 30 times an hour.

This page is the complete table + how the alerter behaves.

---

## Exit code table

| Exit | Class | Meaning | Common cause |
|---|---|---|---|
| `0` | ok | Pipeline completed successfully | — |
| `2` | queue-empty | No pickable issue in the polled state; from `merge-pipeline.sh` also: the merge gate is not yet decided (checks pending or not started, GitHub still computing, a gate read that failed, a hold label `wip`/`blocked`/`needs-human` on the PR, conflicts the rebase stage resolves — never under the shepherd, whose ticket the queue's rebase stage skips) | Normal — happens every tick when there's no work. The queue loop stays quiet; the shepherd waits for the gate (see below) |
| `10` | linear-down | `LINEAR_API_KEY` missing or invalid | Forgot to set it in `.env`, or the key was revoked |
| `11` | worktree-dirty | Uncommitted changes in the worktree; from `spec-pipeline.sh` also: `.specify/feature.json` unusable after speckit-specify | Manual edits in `.worktrees/queue-<mode>/` — clean up before next tick. Spec stage (v3.1): a missing or malformed `feature.json` ends with `11` and routes back to Triage without a label (a re-run may succeed; before, jq's own `2` or `5` escaped, and `2` read as "queue empty"); a stale one — unchanged by the call, naming a directory that existed before it and is not the ticket's own — ends with `11`, routes back to Triage and sets `needs-human`, because a re-run reads the same file. See the troubleshooting guide |
| `12` | no-branch | `bureau-branch` marker missing or points at a non-existent branch | Spec pipeline didn't post a digest, or the branch was deleted |
| `13` | no-tasks | No usable spec for the branch: `tasks.md` missing, no spec directory matches, or the branch fits more than one | `/speckit-tasks` produced an empty file, or the spec directory was renamed or never created: implement and spec review route back to Spec, UX back to Spec Review. On a tie (v3.0.2) implement, spec review and UX name every fitting directory, set `needs-human` and route back, because the stages never guess and re-running Spec cannot resolve a tie: rename or remove the stray directory, then remove the label. When no directory matches the branch at all (v3.1) the three stages also set `needs-human`, and their comment names the branch and the specs directory; a matched directory without `tasks.md` still routes back to Spec without the label |
| `14` | build-failed | Build precondition failed | Test suite red, type-check failed, lint errors, `repo.post_implement_command` failed after an implementation run |
| `15` | no-pr | PR expected but not found | Implement didn't create one, or it was closed manually |
| `16` | provider-unauth | Selected CLI missing or not authenticated | Authenticate the selected Claude/Codex CLI |
| `17` | rebase-needed | `merge_origin_main_or_abort` hit a non-trivial conflict | Routed back to a recovery state for human intervention |
| `18` | gh-failed | `gh` CLI command failed (e.g. `gh pr merge` rejected), or the implement stage's final `git push` failed twice while origin (fetched again) lacks commits of the branch or cannot be read | API rate limit, missing permissions, branch protection, origin rejecting or unreachable |
| `19` | rebase-rejected | `git push --force-with-lease` rejected | Someone else pushed to the same branch concurrently |
| `20` | stopped-before-merge | Review boundary reached | Expected with `--no-merge`, and from the shepherd at Merge under `agents.merge_mode: "manual"`. The shepherd and the queue loop pass a `20` that `--no-merge` asked for without an alert; any other `20` alerts |
| `21` | ownership-conflict | Ticket, checkout or branch held; stale result | Inspect owner/run, preserve work and reconcile |
| `22` | provider-or-result-error | Provider failed or final result invalid | Inspect provider evidence |
| `23` | quota-wait | Selected provider quota reached | Wait for reset; do not switch providers silently |
| `24` | environment-blocked | Permissions or required execution capability missing | Inspect denied operation separately from code/test failures. Also: an npm project's `node_modules` could not be restored after the worktree reset (`npm ci --ignore-scripts` failed twice, or no npm); the last lines of the npm log are in the stage output, the full log sits next to the stamp in `.git/bureau-deps/`. The shepherd halts and alerts. Also: `agents.merge_mode` is `manual` but `linear.teams[0].states.merge` is not set — the review stage refuses at its start, before any paid review; configure the Merge state or set the mode to `auto` |
| `25` | needs-human-or-paused | Halt requiring attention or paused dispatch | Resolve the blocker or unpause explicitly. `merge-pipeline.sh` ends with 25 when its gate is decided against the merge (a failing check, conflicts the rebase stage does not resolve, a stale base, no APPROVE, unresolved threads, a PR that is not open); it posts the blockers on the PR, the queue loop alerts (throttled) and picks the ticket again on the next tick, so a fixed or re-run check still merges without anyone removing a label. The review stage ends every BLOCK (and any unknown verdict) with 25, so a driver never reviews the same commit again. A stage that hands a ticket to a human but cannot write the `needs-human` label ends with 25 where it would have ended with 0 (a stage that already ends non-zero keeps its code) and holds the ticket locally; see the troubleshooting guide. |
| `26` | cancelled-ticket | Ticket cancelled/duplicate | Not successful completion |
| `27` | linear-unusable | A Linear answer stayed unusable after every retry; the stage (or the shepherd's own read of the ticket's state, labels or branch, or its own move of the ticket) stopped instead of deciding on an empty result | Linear outage, an error page, or a query Linear rejects. The shepherd halts: Telegram alert with the fault class (`no-response`, `not-json`, `graphql-errors`, `no-data`), then one attempt each at `needs-human` and a halt comment. Re-shepherd once Linear answers. A queue pick that fails with 27 is reported and alerted, not read as an empty queue. |
| `124` | timeout | Provider exceeded time bound | Inspect preserved progress |
| `130` | cancelled-run | Process interrupted | Inspect ownership and resume evidence |

`bureau-tick.sh` additionally writes a JSON outcome. Quota waiting returns process exit 0 with result `exit_code:23` to allow the next scheduled tick. A stage exiting 0 with unchanged ticket state is `waiting`; only actual Done is `completed`. Shepherd and schedule execution return nonzero for review/human/cancellation boundaries so the next serial ticket does not start as though the first had completed.


Exit codes outside this table (e.g. `1`) classify as `error-1` — usually a bug in the pipeline script or an unhandled bash error.

`shepherd.sh` halts with a Telegram alert on every code except `0` and `2` (carry on) and `10` and `16` (sleep 60 s, retry); a code added later cannot slip through unannounced. `27` additionally tries `needs-human` and a halt comment once each. The one exception is a `20` that `--no-merge` asked for: the shepherd ends with `20` without an alert, and `queue-loop.sh` logs it without one. Under `agents.merge_mode: "manual"`, `merge-pipeline.sh` and `rebase-pipeline.sh` refuse with `2`, and the shepherd's own stop at Merge is a `20` without an alert. The shepherd's own reads follow the same rule: when reading the ticket's state, labels or branch gives up with `27`, it halts the same way and names the read; any other failed read halts with `1` and `needs-human`. A failed read never counts as "no state", "no label" or "no branch". The shepherd's own moves (`--from-stage`, and the bump from Spec to Triage) follow the same rule. Its start check runs before it claims the ticket: a failure there ends with `10` and an alert naming the fault class, and writes nothing to the ticket. Ctrl-C or SIGTERM during a read, a move, a wait or a stage ends the run as a cancelled run (`130`) that writes nothing to Linear but the release of `shepherd-focused` (one attempt); the shepherd's own waits are cut short at once. A SIGTERM sent to the shepherd alone takes effect when the command in flight returns: the usage-throttle wait before a stage and the retry ladder's waits inside a Linear call run to their end first. `--dry-run` runs without the runtime and without these traps: Ctrl-C there ends with `130`, a SIGTERM with `143`. Linear answering five times in a row without a state halts with `1` and `needs-human`. After `--from-stage` a read that does not yet show its target, and after the bump to Triage or a stage that returned `0` a read that still shows the state the ticket left, is read again up to three times, 5 s apart (`BUREAU_SHEPHERD_CONFIRM_SECONDS`), before the next stage starts. A `--dry-run` whose state read fails prints no route and exits with `27` when Linear stayed unusable, `1` on any other failure. At Merge the shepherd follows the merge stage's gate (v3.0.1): a gate that is not yet decided (`2`) is waited for, `BUREAU_SHEPHERD_MERGE_POLL_SECONDS` apart (default 60), up to `BUREAU_SHEPHERD_MERGE_WAIT_SECONDS` in total (default 1800), without the confirmation re-reads and without counting as stuck; a gate decided against the merge (`25`), or one still not eligible when the wait is used up, halts with `25`, `needs-human`, a comment carrying the gate report and an alert. Before, the merge stage ended with `0` whether it merged or not, and the shepherd reported "still reads 'Merge' after the move" and then stuck (`13`). The review stage's inline merge (`BUREAU_INLINE_MERGE=1`) and `merge-pipeline.sh --dry-run` still end with `0` on a gate that did not pass; the review stage does not read the inline result and reports Done (known limitation). A check that never starts, for example on an offline runner, keeps the gate at `2`: the queue loop stays quiet, the shepherd waits its budget and then halts. The wait settings are read in base 10 and capped at 6 h (wait) and 1 h (poll).

Before it claims a ticket, `shepherd.sh` checks whether a human holds it: `needs-human` or the configured `linear.labels.needs_human.name`, `blocked` or `wip` on the ticket, or a local hold that a stage left when it could not write `needs-human` (`<git common dir>/bureau/needs-human-held/<ISSUE>`, checked first, without a Linear read). A held ticket ends the run with `25` and nothing written — no `shepherd-focused`, no `--from-stage` move, no comment, no alert — and a line on stderr names the hold and how to release it; `--dry-run` answers the same way (the hold instead of a route, `25`). A hold left on a finished ticket changes nothing: without `--from-stage` a Done ticket still ends with `0` and a cancelled one with `26`, as in the loop and without any write (the state is read only then), so an orchestrated chain goes on past it. A label read that fails there writes nothing either: `27` with the fault class and an alert when Linear stayed unusable, `1` and an alert for any other failure, `130` for a signal. The loop keeps the same check before every stage and halts with `25` and a comment, as before, now also on the configured name and on a local hold.

**Token-efficiency flags don't change the table.** Under `agents.use_goal_loop: true`, the implement-pipeline still produces the same terminal STATUS values (`COMPLETE` / `PARTIAL` / `NEEDS_HUMAN` / `STUCK` / `CAP_TIME` / `CI_MARKER`) and exits with the same codes the iter-loop path emits — `/goal` swaps the inner control flow but the downstream PR / Linear / exit-code shape is identical. Same for `agents.headroom_wrap` (wraps the claude binary, not the script's exit logic) and `agents.caveman_level` (only affects per-stage prose, not exit codes). See `docs/token-efficiency.md` for the rationale.

---

## Telegram alerts

`alert_telegram` (in `bureau-config.sh`) sends a message when:

1. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` are both set in `.env`
2. The exit code maps to a non-OK class
3. The same issue, pipeline and exit code has not alerted from this repository in the past hour

Throttling lives in `<git common dir>/bureau/alert-throttle.log` of the repository that holds `.bureau.json`: one log per repository, shared by its worktrees, one line per key and time. Delete a line, or the file, to let an alert through again. `BUREAU_ALERT_THROTTLE_FILE` names another file (tests). Only when no git directory resolves does the log stay `/tmp/bureau-alerts.log`, and the key then starts with the repository's path. Before v3.1 every installation on the host shared that one `/tmp` file, so the same key in one repository (a ticket number both teams use, or `none` for a failed pick) silenced another repository's alert for an hour; right after the upgrade the new log is empty, and an alert sent in the hour before may come once more.

When credentials are unset, `alert_telegram` is a silent no-op. Dev environments never break because of it.

### Alert content

A typical alert includes:

- The repository (`Repo:`, the directory name of its main checkout)
- The pipeline that failed (`spec`, `qa`, etc.)
- The Linear issue identifier (`EXP-491`)
- The exit code class
- A short reason from the pipeline (`merge_origin_main_or_abort: non-trivial conflict on src/foo.ts`)
- Tail of the queue log when the supervisor gives up (last 30 lines, see [auto-restart supervisor](#auto-restart-supervisor))

---

## Auto-restart supervisor

`queue-loop-supervised.sh` wraps `queue-loop.sh` and restarts it after a crash with exponential backoff. It only fires a Telegram alert at the *give-up* threshold, not on every restart.

Behaviour:

- Crash 1 → wait 10 s → restart
- Crash 2 → wait 30 s → restart
- Crash 3 → wait 60 s → restart
- Crash 4+ → wait 300 s → restart
- After `BUREAU_SUPERVISOR_MAX_CRASHES` (default 5) consecutive crashes → fire `supervisor` alert with the tail of `logs/queue-<mode>.log` and exit 1
- Counter resets after `BUREAU_SUPERVISOR_STABILITY_WINDOW` seconds (default 1 h) of clean runtime

`SIGINT` / `SIGTERM` are forwarded to the child, so `Ctrl+C` in the tmux pane stops everything cleanly without triggering the restart logic.

To opt out for a debugging session: call `./scripts/queue-loop.sh <mode> <interval>` directly from a workbench pane instead of attaching to the agent window.

---

## Single-flight observability

When `agents.max_concurrent_issues` is non-zero, `spec-pipeline.sh` exits `2` (queue-empty) once the cap is hit — even if there's an eligible issue in Triage. This is intentional: the cap is enforced *before* state mutations.

If you see spec consistently exiting 2 while Triage has eligible issues, check:

```sh
grep "in-flight cap reached" logs/queue-spec.log
```

The pipeline logs the actual count vs. cap on every gate decision.

---

## See also

- [Configuration](configuration.md) — every config knob
- [Troubleshooting](troubleshooting.md) — what to do when an alert fires
