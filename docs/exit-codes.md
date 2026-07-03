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
| `16` | claude-unauth | `claude` CLI not logged in | Run `claude` interactively once to refresh OAuth |
| `17` | rebase-needed | `merge_origin_main_or_abort` hit a non-trivial conflict | Routed back to a recovery state for human intervention |
| `18` | gh-failed | `gh` CLI command failed (e.g. `gh pr merge` rejected) | API rate limit, missing permissions, branch protection |
| `19` | rebase-rejected | `git push --force-with-lease` rejected | Someone else pushed to the same branch concurrently |

Exit codes outside this table (e.g. `1`) classify as `error-1` — usually a bug in the pipeline script or an unhandled bash error.

**Token-efficiency flags don't change the table.** Under `agents.use_goal_loop: true`, the implement-pipeline still produces the same terminal STATUS values (`COMPLETE` / `PARTIAL` / `NEEDS_HUMAN` / `STUCK` / `CAP_TIME`) and exits with the same codes the iter-loop path emits — `/goal` swaps the inner control flow but the downstream PR / Linear / exit-code shape is identical. Same for `agents.headroom_wrap` (wraps the claude binary, not the script's exit logic) and `agents.caveman_level` (only affects per-stage prose, not exit codes). See `docs/token-efficiency.md` for the rationale.

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
