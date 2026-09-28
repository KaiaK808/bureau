# Exit codes & alerts

Every pipeline script exits with a code that classifies its outcome. `queue-loop.sh` reads the code, maps it to an alert class, and (optionally) fires a Telegram message — throttled so a stuck issue doesn't spam you 30 times an hour.

This page is the complete table + how the alerter behaves.

---

## Exit code table

| Exit | Class | Meaning | Common cause |
|---|---|---|---|
| `0` | ok | Pipeline completed successfully | — |
| `2` | queue-empty | No pickable issue in the polled state | Normal — happens every tick when there's no work |
| `10` | linear-down | `LINEAR_API_KEY` missing or invalid | Forgot to set it in `.env`, or the key was revoked |
| `11` | worktree-dirty | Uncommitted changes in the worktree | Manual edits in `.worktrees/queue-<mode>/` — clean up before next tick |
| `12` | no-branch | `bureau-branch` marker missing or points at a non-existent branch | Spec pipeline didn't post a digest, or the branch was deleted |
| `13` | no-tasks | `tasks.md` expected but missing | `/speckit-tasks` produced an empty file; routed back to Spec |
| `14` | build-failed | Build precondition failed | Test suite red, type-check failed, lint errors |
| `15` | no-pr | PR expected but not found | Implement didn't create one, or it was closed manually |
| `16` | provider-unauth | Selected CLI missing or not authenticated | Authenticate the selected Claude/Codex CLI |
| `17` | rebase-needed | `merge_origin_main_or_abort` hit a non-trivial conflict | Routed back to a recovery state for human intervention |
| `18` | gh-failed | `gh` CLI command failed (e.g. `gh pr merge` rejected) | API rate limit, missing permissions, branch protection |
| `19` | rebase-rejected | `git push --force-with-lease` rejected | Someone else pushed to the same branch concurrently |
| `20` | stopped-before-merge | Review boundary reached | Expected with `--no-merge` |
| `21` | ownership-conflict | Ticket, checkout or branch held; stale result | Inspect owner/run, preserve work and reconcile |
| `22` | provider-or-result-error | Provider failed or final result invalid | Inspect provider evidence |
| `23` | quota-wait | Selected provider quota reached | Wait for reset; do not switch providers silently |
| `24` | environment-blocked | Permissions or required execution capability missing | Inspect denied operation separately from code/test failures. Also: an npm project's `node_modules` could not be restored after the worktree reset (`npm ci --ignore-scripts` failed twice, or no npm); the last lines of the npm log are in the stage output, the full log sits next to the stamp in `.git/bureau-deps/`. The shepherd halts and alerts |
| `25` | needs-human-or-paused | Halt requiring attention or paused dispatch | Resolve the blocker or unpause explicitly. The review stage ends every BLOCK (and any unknown verdict) with 25, so a driver never reviews the same commit again |
| `26` | cancelled-ticket | Ticket cancelled/duplicate | Not successful completion |
| `27` | linear-unusable | A Linear answer stayed unusable after every retry; the stage (or the shepherd's own read of the ticket's state, labels or branch) stopped instead of deciding on an empty result | Linear outage, an error page, or a query Linear rejects. The shepherd halts: Telegram alert with the fault class (`no-response`, `not-json`, `graphql-errors`, `no-data`), then one attempt each at `needs-human` and a halt comment. Re-shepherd once Linear answers |
| `124` | timeout | Provider exceeded time bound | Inspect preserved progress |
| `130` | cancelled-run | Process interrupted | Inspect ownership and resume evidence |

`bureau-tick.sh` additionally writes a JSON outcome. Quota waiting returns process exit 0 with result `exit_code:23` to allow the next scheduled tick. A stage exiting 0 with unchanged ticket state is `waiting`; only actual Done is `completed`. Shepherd and schedule execution return nonzero for review/human/cancellation boundaries so the next serial ticket does not start as though the first had completed.


Exit codes outside this table (e.g. `1`) classify as `error-1` — usually a bug in the pipeline script or an unhandled bash error.

`shepherd.sh` halts with a Telegram alert on every code except `0` and `2` (carry on) and `10` and `16` (sleep 60 s, retry); a code added later cannot slip through unannounced. `27` additionally tries `needs-human` and a halt comment once each. The shepherd's own reads follow the same rule: when reading the ticket's state, labels or branch gives up with `27`, it halts the same way and names the read; any other failed read halts with `1` and `needs-human`. A failed read never counts as "no state", "no label" or "no branch". A read cut short by Ctrl-C or SIGTERM ends as a cancelled run (`130`) and writes nothing to Linear. A `--dry-run` whose state read fails prints no route and exits with `27` when Linear stayed unusable, `1` on any other failure.

**Token-efficiency flags don't change the table.** Under `agents.use_goal_loop: true`, the implement-pipeline still produces the same terminal STATUS values (`COMPLETE` / `PARTIAL` / `NEEDS_HUMAN` / `STUCK` / `CAP_TIME` / `CI_MARKER`) and exits with the same codes the iter-loop path emits — `/goal` swaps the inner control flow but the downstream PR / Linear / exit-code shape is identical. Same for `agents.headroom_wrap` (wraps the claude binary, not the script's exit logic) and `agents.caveman_level` (only affects per-stage prose, not exit codes). See `docs/token-efficiency.md` for the rationale.

---

## Telegram alerts

`alert_telegram` (in `bureau-config.sh`) sends a message when:

1. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` are both set in `.env`
2. The exit code maps to a non-OK class
3. The `(issue, class)` pair hasn't already alerted in the past hour

Throttling lives at `/tmp/bureau-alerts.log` — one line per `(issue, class, timestamp)`. Bypass it by deleting the file.

When credentials are unset, `alert_telegram` is a silent no-op. Dev environments never break because of it.

### Alert content

A typical alert includes:

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
