# Troubleshooting

When the pipeline misbehaves, start here. Start with `python3 scripts/bureau-doctor.py` (or the source installer’s `doctor` action before scripts are installed). It reports config provenance, interfaces, effective providers and drift without making remote calls.

---

## Setup

### `MISSING: <tool>` at Phase 0

Install the tool reported for the selected mode, then rerun setup. App tasks need the portable helpers but do not require a background Claude/Codex CLI or tmux. Background mode needs the selected provider executables; GitHub operations need `gh`, and tmux is required only for tmux launchers. Use `python3 scripts/bureau-doctor.py --mode app` or `--mode background` to check the appropriate dependencies.

### `This repo already has a bureau pipeline configured`
- `/bureau-init --update` — change teams / labels / states / agents
- Update the installed Bureau source first, then request scoped interface/script resync in each target repository; see [migration](migration.md)
- Preview the [additive migration](migration.md); preserve existing IDs and settings

### Linear MCP not available during setup
Discover the available Linear connector in the current host. In Claude Code, use `/mcp`; in Codex, use the installed Linear tool capabilities. Shared runtime transitions still require the configured API key.

### `LINEAR_API_KEY` not set
Interactive setup completes without it, but agents fail on first run. Add to `.env` before launching:

```sh
echo 'LINEAR_API_KEY=lin_api_xxxxxxxxxxxx' >> .env
```

---

## Pipeline runtime

### Agents running but not picking up issues
Check the issue:
- Has the eligibility label (e.g. `lane-2`) — `pick_issue` filters by label *name*
- Is in the `Triage` state for the team configured in `.bureau.json`
- Has no parking labels (`needs-human`, `blocked`, `wip`)
- Is not blocked by another issue (Linear `blockedBy` relation)
- In Build Review or Merge: another ticket may go first while this one waits on its merge gate (the queue log says `pick: TEAM-123 waits on its merge gate …`; see [A ticket waiting on its merge gate is picked after the others](#a-ticket-waiting-on-its-merge-gate-is-picked-after-the-others))

### Pipeline exits 2 every tick despite eligible issues
Check whether `agents.max_concurrent_issues` is set. If yes, an in-flight issue (or several) is occupying the cap. See [recipes → single-flight](recipes.md#single-flight-pipeline). Find them with:

```sh
grep "in-flight cap reached" logs/queue-spec.log
```

### `⚠ bureau-init template drift: N differ, M new`
The skill template has been updated since this repo was initialized. Run:

```sh
# In the current assistant: $bureau-init --resync-scripts (Codex)
# or /bureau-init --resync-scripts (Claude)
```

Inspect the deterministic preview. Unknown/customized files stop the selected asset batch; resolve each named conflict while preserving local changes, then reapply the coherent batch. `--update` changes configuration only. See [migration](migration.md) for source update and per-target resync steps.

### Issue stranded between states
The spec pipeline installs an EXIT trap that routes back to Triage on crash. If an issue is genuinely stuck:

1. Check `logs/escalations.log` and the latest issue comment — background escalation sites log after a successful human-label update. `grep ESCALATED logs/escalations.log | grep EXP-XXX`.
2. Check `logs/queue-<stage>.log` for the last error.
3. Check `logs/supervisor-<stage>.log` if you're using `queue-loop-supervised.sh`.
4. Inspect `bureau-runtime.py status` and the saved run before reconciling state; stale results cannot safely advance it.

If the issue was in Build and crashed, the worktree under `.worktrees/queue-implement/` may have uncommitted work. Inspect before resetting.

### `could not add 'needs-human'` — a ticket held without its label
A stage that hands a ticket to a human adds `needs-human`, and the picker leaves labelled tickets alone. When that label write fails, the stage records the ticket in a local hold, `<git common dir>/bureau/needs-human-held/<ISSUE>` (one file per ticket; the common dir is that of the repository holding `.bureau.json`, so every worktree and every caller sees the same holds wherever it runs from; the pause marker `bureau/paused` is found the same way), alerts with the code the stage ends with, still posts its comment, and ends non-zero: 25 where it would have ended with 0, its own code otherwise (17, 18, 19), 27 when Linear stayed unusable. Every pick skips held tickets and tries the label again; once the label is on the ticket the file is removed and the label keeps the ticket out, so you release it the usual way, by removing the label. A label that still cannot be written never fails the pick: the ticket stays held and skipped and the pick goes on. Each retry is one attempt without waits, and after one such 27 the other holds wait for the next pick. The picker's notes (held and blocked tickets, retries) are in the queue log. If `BUREAU_CONFIG` points outside any git repository, holds and the pause marker fall back to the current directory's repository and a warning says so. If the label can never be written (for example, no label named `needs-human` exists for the team or the workspace), fix that, or delete the file to release the ticket without the label. `shepherd.sh` reads the hold before it claims the ticket and refuses a held one with 25 (see [`shepherd.sh` refuses a held ticket](#shepherdsh-refuses-a-held-ticket--exit-25-before-the-claim)); a stage started with a ticket key (`stage.sh EXP-123`) does not read it.

An implement halt (`PARTIAL`, `STUCK`, `CAP_TIME`, `NEEDS_HUMAN`, `CI_MARKER`) whose label could not be written now ends with 25 instead of 0. If the worktree holds work that is not on origin, `bureau-worker.sh` preserves it as unfinished work, and later implement runs in that worktree end with 21 (ownership conflict) until a human resolves it. Before, the 0 let the next reset discard that work. Preserving it and stopping loudly is intended, as after a review BLOCK: push or drop the preserved work, write the label (or delete the hold), then resume.

A pick in `queue-loop.sh` that fails because Linear stayed unusable is logged as `pick failed: Linear stayed unusable (exit 27)` and alerts (throttled per stage per hour in each repository) instead of reading as an empty queue; any other pick failure is logged and still treated as an empty queue.

### Pipeline keeps hitting `merge_origin_main_or_abort` conflicts
Symptom: every cron tick re-runs the merge, conflicts pile up, the same branch reappears tick after tick.

Two complementary fixes:

1. **Single-flight mode** (`agents.max_concurrent_issues: 1`) — branches don't race against each other.
2. **Trivial-conflict resolver** — already shipped; lockfiles, generated files, and known-mergeable patterns auto-resolve.

Non-trivial conflicts stop the stage; inspect its exact exit class and preserved checkout. `upstream-port.sh` uses exit 17 for patch conflicts. Do not repeatedly reset or rerun a conflicted worker.

---

## Exit-code symptom map

Each pipeline script exits with a classified code — see `docs/exit-codes.md` for the canonical table. What to do when you see each:

### Exit 10 (linear-down) — Linear API unreachable or auth-rejected

The Linear precondition reports exit 10 when the API check fails. Inspect its response and the trusted API-key configuration before changing ticket state; do not diagnose it by starting a model.

- Check `.env` — `LINEAR_API_KEY` set and not expired?
- Check Linear's status page. Rare, but happens.
- `curl -sS -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" -d '{"query":"query { viewer { id } }"}' https://api.linear.app/graphql` — should return your user id

### Exit 27 (linear-unusable) — Linear answered, but nothing usable

Every Linear fetch checks the answer before a stage decides on it and retries an unusable one (10, 30, 60 s by default). Exit 27 means it stayed unusable. The log names one fault class per attempt, never the answer text:

- `no-response` — curl failed, the request hit its time limit (logged as "no answer within Ns"; `BUREAU_LINEAR_MAX_TIME`, default 30 s), the body was empty, or the HTTP status was outside 2xx.
- `not-json` — the body is not one JSON object, or it contains a NUL byte.
- `graphql-errors` — the answer carries `errors` (an invalid key usually shows up as exit 10 earlier).
- `no-data` — no `data` object, an empty one, a root field that is `null`, or an issue answer without the list the reader needs (labels, state, comments; for the queue picker also each ticket's blockers, for the in-flight count each ticket's children). Before v3.1 the picker and the count read such a missing list as empty: the picker could take a `needs-human` or blocked ticket, and the count reported 0 in flight.

Check Linear's status page and the key as for exit 10, then re-run the stage. A ticket that does not exist is not exit 27: its readers answer with an empty result.

### Exit 11 (worktree-dirty) — uncommitted changes block progression

The stage requires a clean checkout but finds local changes, possibly preserved from an interruption.

```sh
python3 scripts/bureau-runtime.py status
git worktree list
# Inspect the exact workspace reported by the run:
git -C "$BUREAU_WORKSPACE" status --short
```

Inspect the owner and save the work before deciding how to resume. Existing unregistered workers and app checkouts cannot be made disposable by clearing files. Do not reset, clean, forcibly remove or detach another task's checkout. See [ownership and resume](stage-protocol.md).

**From the spec stage: `.specify/feature.json` after speckit-specify.** The spec stage reads the feature directory from `.specify/feature.json` once speckit-specify has run, and ends with `11` when it cannot use it:

- *Missing or malformed* (no file, not valid JSON, no `feature_directory`, or a directory that does not exist): the comment says "absent or invalid", the ticket goes back to Triage without a label, and the next pick tries again. v3.0.2 ended here with jq's own status instead (`2` for a missing file, `5` for invalid JSON), and `2` reads as "queue empty": the queue took the ticket again on the next tick and paid another specify run.
- *Stale* (v3.1): the file holds byte for byte what it held before the call (compared by checksum; a stale value written back during the call, by an agent rewriting the file or by `git checkout` of it, counts as unchanged), the directory it names existed before the call, and that directory is not the ticket's own. The ticket's own directory is the one in the specs directory named exactly like the last segment of the ticket's newest `bureau-branch` marker (the spec stage names the branch, and so the marker, after its directory), or of the branch checked out before the call (a disposable spec worker starts detached, so in the queue only the marker counts). Nothing is matched loosely here: on a re-spec the worker starts on `origin/main`, where the ticket's own directory, still on its unmerged spec branch, is missing, and a loose match on the marker would take a sibling (`003-auth-sso` fits `002-auth`). Linear's generated branch name does not count. Plan and tasks would have written into another ticket's spec, so the stage stops before them, routes back to Triage, sets `needs-human` and alerts. A re-run reads the same file, so removing the label alone repeats the hold. The usual cause: the file is tracked and `main` holds the last merged feature's value, and speckit-specify did not record the new feature (its branch hook was skipped, or the run failed early). Check the spec run's output and `git log -1 -- .specify/feature.json` on `main`, and make speckit-specify write the file (or run the spec by hand). If the named directory is this ticket's after all (a spec made by hand, or a marker that got lost, and speckit-specify recorded the same value again), the stage cannot tell it from a stale file by name and holds it the same way: post a comment on the ticket whose first line is `<!-- bureau-branch: <directory name> -->` for that directory, so the next run counts it as the ticket's, then remove `needs-human`. A file whose content speckit-specify changed is trusted, even when it names an older directory.

### Exit 12 (no-branch) — `bureau-branch:` marker missing

Downstream stages resolve the branch via a `<!-- bureau-branch: ... -->` marker comment posted by spec-pipeline. Exit 12 means the marker is absent or points at a non-existent branch.

- Check the Linear issue's comment history — is the digest comment there?
- Reconstruct a missing marker only from the verified issue branch and completed spec evidence. Reconcile the issue state and use the selected app stage or named-ticket driver; `queue-loop.sh spec 1` polls all eligible tickets at a one-minute interval and is not an issue-specific repair.

### Exit 13 (no-tasks) — `tasks.md` expected but missing or unmatched

Confirm the approved feature directory contains `tasks.md` and matches the issue's canonical branch. Do not infer a `codex/*` branch from Linear's generated branch name. For retained Spec Kit helpers, pass both `SPECIFY_FEATURE` and `SPECIFY_FEATURE_DIRECTORY` for that exact feature; see [legacy migration](migration.md). Inspect ambiguous numeric prefixes instead of guessing another feature.

Every stage finds the spec directory with `bureau_spec_dir_for_branch` (`bureau-config.sh`). It first drops everything up to the branch's last `/` and a leading issue key such as `exp-1444-`, then takes: a directory named exactly like the branch or its last segment; else the one directory whose slug (the name without `NNN-`) fits the branch slug — equal, the branch slug as its start up to a `-` (a truncated branch name), or a run of whole `-`-separated words inside the branch slug; when several fit, the branch's number narrows them first (the fits that carry it, if any, are the only ones left), and among the fits left the one whose slug equals the branch slug wins (v3.1: `codex/exp-9-report-builder-wireup` takes `128-report-builder-wireup` over `117-report-builder`, where v3.0.2 stopped on a tie; a branch cut down to exactly a shorter directory's slug therefore takes that directory). A number alone never selects a directory. Anything else yields no directory: implement stops here rather than build from another ticket's `tasks.md`, and when two directories fit equally, implement, spec review and UX name them in their comment and set `needs-human`. When no directory matches the branch at all, the three stages set `needs-human` as well (v3.1) and name the branch and the specs directory; a directory that matches but lacks `tasks.md` goes back to Spec without the label, because `/speckit-tasks` can repair that. To see what a stage will pick, run `bash -c 'source scripts/bureau-config.sh; bureau_spec_dir_for_branch 001-my-branch; bureau_spec_dir_candidates 001-my-branch'` from the repository root. Typical causes: the spec directory was renamed after the branch was created, the branch shares no word with its spec directory, or two directories fit equally (`001-export-csv` and `001-export-csv-v2` for the branch `001-export-csv-v2-fix`) — rename or remove the stray directory, then remove `needs-human`.

**A ticket got its parent ticket's spec (known limitation).** The stages read the branch from the ticket's `bureau-branch` marker comment; without one they fall back to Linear's generated branch name, `user/<key>-<title-slug>`. When a child ticket's title starts with the parent ticket's slug (title "Split config loader phase 2: CLI flags", parent directory `020-split-config-loader`), the matcher takes the parent's directory, because the parent's slug is a whole-word run of the title and the child's own directory slug is not. v3.0.1 gave the same answer. The marker is canonical: if a stage names the wrong directory, check that the newest spec comment on the ticket starts with `<!-- bureau-branch: <branch> -->` and restore it from the ticket's real branch before re-running.

### Exit 14 (build-failed) — implement / upstream-port build gate failed

The build command (`cargo build`, `npm run build`, `bash tests/run.sh`, etc.) returned non-zero. For **upstream-port.sh**, the command comes from `repo.upstream_port.build_cmd` — verify it's right for the target repo.

- Tail: `tail -40 /tmp/upstream-port-build.*` (or check the pipeline log)
- Common culprit: your build depends on a service (postgres, redis) that isn't running in the worktree

### Exit 15 (no-pr / test-failed) — test gate failed OR expected PR doesn't exist

**Overloaded code.** In queue-loop.sh scripts: no PR was found for the branch after the stage that should have created one. In upstream-port.sh: the test command (`repo.upstream_port.test_cmd`) returned non-zero.

- For "no-PR": check `gh pr list --head <branch>` — was one created? Was it closed manually?
- For "test-failed": look at the tail of `/tmp/upstream-port-test.*` for the failing test names

### Exit 17 (rebase-needed) — `git apply --3way` produced conflicts

In **upstream-port.sh**, configured path translations have been applied but `git apply --3way` still has content conflicts. Inspect the specific caller when this class appears elsewhere.

- If appropriate, retry with `--with-llm` for the configured `upstream_port` provider; the explicit cost gate applies and non-interactive use also requires `--yes`
- Or escalate to a full shepherd ticket: create a Linear issue with the upstream link and let the normal pipeline handle it

### Exit 18 (gh-failed) — overloaded catch-all for `gh` failures

`upstream-port.sh` uses exit 18 for GitHub failures and preflight errors such as invalid arguments, dirty checkout or an existing branch. Other scripts can propagate a different status; read the emitting command.

- First: `gh auth status` — token expired?
- Rate limit: inspect `gh api rate_limit` and the reported reset time
- Branch protection: check if the PR needs a review approval you don't have (bot accounts sometimes hit this)

### Exit 19 (rebase-rejected) — `git push --force-with-lease` refused

`rebase-pipeline.sh` uses `--force-with-lease` for safety. Refusal means someone else pushed to the same branch since your `git fetch`.

- Preserve the local rebased HEAD, fetch and compare the newly published commits before any retry. Refreshing the remote-tracking ref and immediately force-pushing can defeat the protection that just stopped the run.
- Reconcile the other writer's work and ownership before a separately authorized push.

### Exit 21 (ownership-conflict) — needs-human and one comment

A stage or the shepherd stops with 21 when the run may not take the ticket or its worktree: another run holds the ticket or the worktree (the runtime's claim), the worktree exists but is no registered Bureau worker (preserved from an interrupted or unfinished run or from a review stopped before merge, a directory Bureau did not create, or the main checkout), a registered worker now holds another repository, or the ticket's branch is checked out in another worktree. Since v3.1.0-rc.2 such a halt sets `needs-human` (with the local hold when the label cannot be written, see [`could not add 'needs-human'`](#could-not-add-needs-human--a-ticket-held-without-its-label)) and posts one comment that names the worktree and the way back; stderr carries the same steps. The queue then skips the ticket instead of hitting the same conflict on every tick, where before the ticket stayed in its state with nothing on it.

- The comment goes to the ticket whose run preserved the worktree when Bureau recorded that (`<git common dir>/bureau/preserved/`, written when a worker loses its registration). In a queue that shares one worktree per stage that can be another ticket than the one just picked; the picked ticket then gets nothing. Without a record the first ticket that hits the worktree carries the halt, and later tickets get nothing.
- A repeat sets the label again but posts no second comment (`<git common dir>/bureau/ownership-halts/<ISSUE>.<key>`); both records are removed once the worktree is reset as a registered worker again.
- Nothing is written for a cancelled run, or when the holder is a live run: the second run ends with 21 and names the run that is still active.
- A 21 for another reason writes nothing, as before: the state changed during the stage, a stage started outside a disposable worker, a git command failed inside the reset, or a stage's own branch check at its start.

Follow the comment (for an interrupted run, the steps under [`shepherd.sh` bailed mid-run](#shepherdsh-bailed-mid-run--how-to-resume)), remove `needs-human` and rerun.

### Exit 124 (timeout) — a provider pass hit its time limit

A provider call that runs past its limit is stopped: the adapter ends its process group, and the stage gets 124. A shell the agent started detached (Claude's Bash tool does so) is outside that group and can keep running for a while; see [provider runtime](provider-runtime.md). Before v3.2 the implement stage ended on the first such pass, although in the field the agent had usually finished, committed and pushed every task and was only polling CI until the limit killed it.

**Implement (since v3.2):** a pass that times out is counted like any other pass, and the loop goes on. The pass appears in the iteration log, on stdout and in the summary comment, as `iter N: status=TIMEOUT tasks_done=M commits=K (timed out after Ss)`: `commits` comes from Git as for every pass, `tasks_done` is the number of tasks the pass marked `[X]` in `tasks.md` (it reported no status block), and stderr says `iter N timed out after Ss (exit 124); it counts as a pass like any other`. The squash-range check and the push (or its deferral, see `agents.implement.push_each_iteration`) run after it as after any pass. The next pass gets a note at the top of its prompt ("The previous pass timed out"): it checks `tasks.md` and the branch first and reports `COMPLETE` at once when nothing is left: every task marked `[X]`, its work in the commits on the branch, and any feedback above the note (the "Feedback to address" block) addressed: in a rework every task is `[X]` already, so the feedback decides. A Claude pass is also asked to commit finished work the stopped pass left uncommitted, and to remove a git `index.lock` only when no git process has the worktree as its working directory (`lsof -a -c git -d cwd`, or `pgrep -l git`), else to report `NEEDS_HUMAN` naming the lock; after a Codex pass the shell has already committed, and the note says so. The stage ends with 124 only when no pass is left after a timed-out one: `BUREAU_IMPL_MAX_ITER` passes have run, or `BUREAU_IMPL_TOTAL_TIMEOUT` leaves 60 s or less for another. Then stderr says `Provider pass failed with exit 124 (iter N timed out) and no pass is left: …` with the reason, and every commit origin lacks is pushed before the stage ends. Every other non-zero provider exit (16, 22, 23, 24, 130) still ends the stage right after its pass, as before, and since v3.2 also pushes every commit origin lacks first, the failed pass's own included, whatever `agents.implement.push_each_iteration` says (before, the failed pass's commits stayed in the worktree with `true`, and with `false` when it was the first pass): stderr says `pushing N commit(s) of <branch> that origin lacks before the stage ends (provider exit X)`, stdout `pushed <branch> (provider exit X): N commit(s) origin lacked, head <sha>` once it went through. The stage keeps the provider's exit code, also when stderr is gone (a closed terminal, a queue loop's tee that ended); a refused push is reported on stderr and the work stays in the worktree. A stage that ends before its end-of-run push in any other way (a signal, a crash, an unusable Linear, a failed `/goal` run) pushes the same way from its EXIT trap, labelled `the stage ends before its end-of-run push` (see `agents.implement.push_each_iteration` in the [configuration](configuration.md)).

So an implement stage can now take up to `BUREAU_IMPL_MAX_ITER` provider passes when passes time out, together bounded by `BUREAU_IMPL_TOTAL_TIMEOUT` (each pass gets `BUREAU_IMPL_ITER_TIMEOUT`, cut to what is left of the total). With the defaults that is at most 5400 s of provider time where a stage used to end after the first 1800 s pass. Two settings bring back the old ceiling, each at a cost. `BUREAU_IMPL_TOTAL_TIMEOUT` equal to `BUREAU_IMPL_ITER_TIMEOUT`: a timed-out pass leaves no budget for another and the stage ends with 124 as before, but a productive run whose passes end in time (`PARTIAL`, then more) also gets no more than that one pass's time in total. `BUREAU_IMPL_MAX_ITER=1`: one pass of any kind, so a `PARTIAL` pass is never followed up either. The `/goal` path (`agents.use_goal_loop`) is unchanged: one call bounded by `BUREAU_IMPL_TOTAL_TIMEOUT`, and a timeout ends the stage with 124.

**What the pass did:** `stdout.log` in the provider evidence is empty after a Claude timeout, because `claude -p --output-format json` writes its result only at the end. Since v3.2 the adapter starts every Claude call with its own `--session-id`, and `result.json` records it with the transcript Claude writes while it works:

```sh
jq '{outcome, session_id, transcript, transcript_found}' logs/provider-runs/RUN/result.json
tail -n 20 "$(jq -r .transcript logs/provider-runs/RUN/result.json)" | jq -c '.message.content? // .'
```

The stage log names the same path in a line `Bureau provider transcript: …` next to `Bureau provider evidence: …`. A Claude CLI that does not list `--session-id` in `claude --help` runs without it, as before v3.2; then `session_id` is `null`, `transcript_found` is `false`, and `transcript_note` says why. A CLI that refuses the flag although it was cached as taking it (a downgrade behind an unchanged wrapper) costs no failed stage: the call runs again without the flag, and `result.json` says `session_id_rejected: true`. For Codex, `session_id` is the thread id from the first `thread.started` event in `stdout.log` (Codex streams its events, so `stdout.log` holds the run up to the timeout) and `transcript` its rollout file under `~/.codex/sessions/` (`CODEX_HOME`), when it is there. See [provider runtime](provider-runtime.md).

**Why a pass polls CI at all:** a project instruction such as "a missing CI run means do not merge, wait for it or trigger it" in the repository's `CLAUDE.md` reads to the agent as its own job. Since v3.2 every stage's system text, and `scripts/bureau-stage.md`, say never to wait for, poll or re-trigger CI or a merge gate inside a stage and never to commit CI results as evidence, that a missing, pending or failed CI run does not change the status the agent reports for its own work (not merging is all a rule like "do not merge without green CI" asks of a stage, so the agent does not turn to `NEEDS_HUMAN` instead), and that this rule wins over project instructions. Waiting on CI is the merge gate's and the shepherd's job. If a transcript still shows `gh pr checks` or `gh run view` loops, check that the main checkout runs the v3.2 scripts (the system text comes from there) and that no stage prompt of your own asks for the wait.

### Exit 1 / 128 / 141 — `error-<N>` catch-all

Anything outside the classified table maps to `error-<N>` in `queue-loop`'s alert throttling. Usually a bug in the pipeline script or an unhandled bash error.

- `128` from git: inspect `git worktree list` and runtime ownership. A held branch requires a coordinated handoff, not forced removal of the other checkout.
- `141` indicates SIGPIPE. Inspect whether a consumer exited early and whether the stage produced a valid result; do not count this as a successful stage automatically. Before v3.2 three stage lines could end this way on a long text: the build review right after the paid review (exit 141, or 1 with `printf: write error: Broken pipe`), the spec stage's research check (no exit, the digest was dropped as "research produced no valid output") and `upstream-port.sh`'s commit-title fetch (exit 18, "could not fetch commit message"). v3.2 reads those texts without a pipe; on an older installation rerun the stage.
- `1` catch-all: read the last 20 lines of stderr; that's where the actual error will be.

---

## Driver failures (shepherd / orchestrate / upstream-port)

### How to stop a run

A shepherd run, and a stage a queue worker started, is a chain of processes: the runtime in front (`bureau-runtime.py … exec`, which `shepherd.sh` and `bureau-worker.sh` turn into as they start), the shepherd, the worker, the stage, the provider adapter and the agent. Each wrapper starts the next one in a session and process group of its own, forwards a stop to it and waits a grace period before it kills that group, each one step longer than the wrapper inside it (`BUREAU_STOP_GRACE_SECONDS`, 20, 15, 10 and 5 s by default). A stop that reaches the runtime in front therefore reaches every process of the run. These ways do that:

- **Ctrl-C** in the run's terminal or tmux pane.
- **Close the terminal, the ssh connection, the tmux window or the tmux session**, for example `tmux kill-session -t bureau-shepherd-<repo>` or the session `start-bureau-v2.sh` opened. Since v3.2 a hang-up (SIGHUP) stops the run like SIGTERM. The runtime in front forwards it as SIGTERM, so a stage or an agent that ignores SIGHUP stops too, and the shepherd, the worker, the provider adapter and the queue supervisor end on a SIGHUP of their own as on SIGTERM. Until v3.1 a hang-up ended only the runtime in front: the shepherd, the worker and the stage ran on without a terminal and kept the claim until someone stopped their process groups by hand. Detaching from tmux (`Ctrl-b d`), or closing a terminal window that only shows an attached tmux client, hangs up nothing: the tmux server keeps the pane, and the run goes on.
- **`kill -TERM <pid>`** from another terminal, where `<pid>` is the runtime in front: `python3 scripts/bureau-runtime.py status` lists the run's leases, and their `pid` is that process. It works wherever the run was started. `kill -TERM -- -<pid>`, the runtime's whole process group, does the same only while the runtime leads its own group, which `ps -o pid=,pgid= -p <pid>` shows as two equal numbers. That holds for a shepherd that opened its own tmux window (started without `--no-tmux`) and for a command typed at an interactive shell prompt, in a terminal or a tmux pane, since the shell's job control gives each command a group of its own. It does not hold when a script, cron or another program started the shepherd as its child, nor for the runtime of a queue stage, which runs in the queue supervisor's group: there `kill` answers "No such process" and nothing stops.
- **A queue**: Ctrl-C in its pane, closing the pane, its window or its session, or `kill -TERM -- -<supervisor pid>`, the supervisor's whole process group (it leads one when it was typed into the pane's shell, as `start-bureau-v2.sh` does). The signal reaches the supervisor, the queue loop and the runtime in front of the stage in flight, and that runtime stops the stage as above.

Each of them ends the run with exit 130, a cancelled run, and nothing of it is left running once the grace periods are over. The shepherd writes nothing to Linear but the release of `shepherd-focused` (one attempt; when Linear fails right then, remove the label by hand). The work stays for the resume: the runtime keeps the run's leases and marks them interrupted, the worktree keeps every file and commit the stage made and loses its disposable-worker registration so that no later reset erases it, and an implement stage tries to push every commit origin lacks on its way out when the signal stops it (since v3.2 with either value of `push_each_iteration`, the commits of the pass that was running included, and also when its terminal is gone). When a queue's log pipe goes first (its `tee` ends with the pane, for example on Ctrl-C or when the pane closes) and the stage writes to it before the signal reaches the stage, the write ends the stage at once (SIGPIPE, exit 141); its EXIT trap still pushes. A commit that did not reach origin (a refused push, a push cut by the stop grace) stays in the preserved worktree, and step 2 of the resume pushes it. The resume steps are printed once on stderr. After a closed terminal they are gone with it; `python3 scripts/bureau-runtime.py status` then shows the run's leases with `"interrupted": true`, and the steps are the ones below, in [`shepherd.sh` bailed mid-run — how to resume](#shepherdsh-bailed-mid-run--how-to-resume).

These do not stop a run cleanly:

- **SIGTERM or SIGHUP to an inner process or process group only.** The runtime in front gets no signal. Since v3.2 it counts a 130 from its child as a stop all the same (it keeps the leases interrupted and prints the steps), but the run does not end the way a clean stop ends it. It ends in one of these four ways, depending on which process got the signal:
  - **The shepherd's own group** (the one under the runtime in front; the worker's runtime runs in it as well). The shepherd releases the claim and ends with 130, and the stage stops through the worker's runtime. The shepherd can end first, before a stage with a slow way out has stopped (implement's deferred push, a build that cleans up), and the runtime in front ends with it: the steps are printed while the stage is still stopping. Run `python3 scripts/bureau-runtime.py release RUN_ID` until it no longer answers "Background process group still alive", then follow the other steps; `status` cannot tell, since it lists the run's leases and their `pid` and says nothing about whether those processes still run. Until v3.1 whether the leases were kept depended on that order: kept when the stage had stopped before the shepherd ended, released while the stage still ran when the shepherd ended first, or when no stage ran (the shepherd stopped during a Linear read or its own wait), and no steps were printed then.
  - **The worker's group** (or the stage's runtime in it). The stage stops and the worker ends with 130, but the shepherd got no signal: it takes the 130 for a stage that halted, sends a Telegram alert `shepherd halt (cancelled-run)`, releases the claim and ends with 130. The leases are kept interrupted and the steps are printed, as after a clean stop. Nothing else is wrong; ignore the alert and follow the steps.
  - **The stage's own process group.** The stage dies of the signal, and its runtime, which got none, ends with 256 minus the signal's number (241 for SIGTERM, 254 for Ctrl-C's SIGINT). The worker and the shepherd take that code for a failed stage: the shepherd halts with a Telegram alert `shepherd halt (error-241)` (or the code it got), and the runtime in front releases the leases, as after any halt. When the stage left changes or commits, the worker keeps the worktree as unfinished work.
  - **On bash 3.2 (macOS), the shepherd's subshell.** The shepherd runs the worker in a subshell (`shepherd.sh`, the call to `bureau-worker.sh`), a process of its own with the same command line as the shepherd. A SIGTERM to that process alone ends it, the shepherd takes its 143 for a failed stage, halts with an alert and ends with 143, and the runtime in front releases the leases while the stage still runs under the worker.

  Always send the stop to the lease `pid`, the runtime in front.
- **SIGTERM or SIGHUP to the queue supervisor's process alone** (`kill -TERM <supervisor pid>`). The supervisor sends SIGTERM to the queue loop's process only and exits; the loop ends, but the runtime in front of the stage in flight gets nothing, and the stage runs to its end without a queue around it (as in v3.1). Stop the whole group instead, as above, or the stage's runtime with `kill -TERM <pid>`.
- **`kill -9` (SIGKILL) of the runtime in front.** It cannot be forwarded: the rest of the run goes on without it, as after a hang-up before v3.2, and its leases are not marked interrupted.
- **`python3 scripts/bureau-runtime.py pause`.** It stops dispatch: the queue starts no new stage, and a stage in flight runs to its end. Pause first, then stop the queue sessions, so that no new stage starts while you close them.

A run started under `nohup` ignores the hang-up on purpose and outlives its terminal, as before v3.2; stop it with `kill -TERM <pid>` as above.

### `shepherd.sh` bailed mid-run — how to resume

Inspect the issue's current state, blocker comment, saved run and checkout. Resolve the cause and any human label before rerunning `bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123`. The state guard skips completed stages, but an unfinished/unregistered checkout can still refuse reuse; preserve its work and reconcile ownership first. Use a dry-run to inspect routing only. Exit 20 is the requested review stop, 25 is pause/human attention, and 26 is a cancelled ticket; none is Done.

**After an interrupted run (exit 130).** A run stopped by Ctrl-C, SIGTERM or a hang-up (see [How to stop a run](#how-to-stop-a-run)) keeps its leases and its worktree, and the worktree loses its disposable-worker registration, so that no later reset erases the work. Releasing the run is therefore not enough: a rerun on the same worktree stops with 21 ("refusing to reset unregistered worktree"), and a spec stage's feature branch that was never pushed stays behind in the repository. The interrupt message names the run, the ticket and the worktree, and prints the steps once, with the actual run ID, paths and branch:

1. Release the run's ownership: `python3 scripts/bureau-runtime.py release RUN_ID`. While a process of the run is still alive, `release` refuses with "Background process group still alive; stop it before releasing": run it again until it no longer gives that answer. `python3 scripts/bureau-runtime.py status` cannot tell: it lists the run's leases and their `pid` and says nothing about whether those processes still run. (The printed step still says to check `status` first.)
2. Save anything you want from the worktree, then drop it: `git worktree remove --force WORKTREE`. The message says what to do with its branch. A branch that was never pushed and has no commits of its own (the spec stage's fresh branch) is deleted, `git branch -D BRANCH`, or the next spec run finds its name taken. Commits that are on no remote, or not on `origin/BRANCH` (an implement stage whose final push failed), are pushed first, `git push -u origin BRANCH` or `git push origin BRANCH`, never deleted; a never-pushed branch is then kept as `BRANCH-saved` (`git branch -m`), since the rerun creates `BRANCH` again. A branch origin has needs no deletion: the rerun's `git checkout -B` resets it. Or keep the worktree and rerun with a new one instead: `shepherd.sh --worktree DIR` (a worktree of its own: the shepherd refuses the main checkout, `--worktree .`, with exit 1 before it claims anything).
3. Rerun the shepherd or the stage.

The runtime gives a stage time to end after the signal before it kills it: 10 s for a stage under the shepherd, 15 s under a queue worker, since each wrapper waits 5 s longer than the one inside it (`BUREAU_STOP_GRACE_SECONDS` sets the step). Until the stage has ended, `release` refuses. A rerun that skips step 1 stops with 21 at the claim, one that skips step 2 stops with 21 at the reset; either way the ticket gets `needs-human` and one comment with the same steps (see [Exit 21](#exit-21-ownership-conflict--needs-human-and-one-comment)), so remove the label after the fix. Before v3.1.0-rc.2 the message said only "work preserved; inspect processes and explicitly release ownership", printed once per nested wrapper, and the halt at 21 left nothing on the ticket.

### `shepherd.sh` refuses a held ticket — exit 25 before the claim

Before it claims a ticket (the `shepherd-focused` label) or moves it (`--from-stage`), the shepherd checks whether a human holds it, and a held ticket ends the run with 25 and nothing written: no claim, no move, no comment, no alert. A ticket is held when it carries `needs-human`, the configured `linear.labels.needs_human.name`, `blocked` or `wip`, or when a stage could not write `needs-human` and left a local hold, `<git common dir>/bureau/needs-human-held/<ISSUE>` (checked first; it needs no Linear read). The line on stderr names the hold. To release it, remove the label in Linear; for a local hold, the next queue pick writes the label and removes the file, or delete the file to release the ticket without the label. Then re-shepherd. `--dry-run` prints the hold instead of a route and ends with 25 as well. A label left on a finished ticket (a human closed a ticket the shepherd had halted on) does not count: without `--from-stage` a Done ticket ends with 0 and a cancelled one with 26, as always, so an orchestrated chain goes on past it; with `--from-stage` the ticket would move back into the pipeline, and its hold refuses it. A label read that fails before the claim writes nothing either: 27 when Linear stayed unusable (the alert names the fault class), 1 for any other failure, 130 for Ctrl-C. The loop checks the same holds before every stage; there a hold halts with 25 and a ticket comment, as `needs-human` always did. Until v3.1 the shepherd claimed and moved a held ticket first, and a configured label name or a local hold did not stop it at all.

### `orchestrate.sh --execute` — one lane failed, do the others continue?

**Yes** — independent lanes can continue. A serial chain stops at its first nonzero result, including review-stop 20 and human-attention 25. Dependent tickets are not complete merely because their predecessor reached review.

To recover:

```sh
# See lane status
./scripts/bureau-status.sh

# Rerun a specific lane
./scripts/shepherd.sh --no-tmux --no-merge TEAM-9 --worktree .worktrees/shepherd-lane-2
```

Inspect that lane's saved work and ownership before reuse. After resolving the blocker, recheck the schedule and current ticket states; completed tickets are skipped, but preserved workers are not automatically reset.

### `upstream-port.sh` exit 15 — test gate false-negative

Sometimes the test command passes locally but not inside the worktree — usually a service dependency (postgres, redis) or a missing env var.

- Tail the failing test names from the log
- If it's environment: set the needed env in `.env` and re-run
- If flaky, reproduce and record the failure. Correct the test or document any explicitly accepted exclusion; silently skipping it does not establish a passing acceptance gate.

---

## Stage-specific failures

### Merge gate refuses to merge a PR that looks green

`merge-pipeline.sh` enforces THREE independent gates: `pr_ci_is_green` (all check-runs completed + success), `pr_base_is_current` (`baseRefOid == origin/main HEAD`), and `mergeStateStatus == CLEAN` (GitHub's own answer). All three must pass.

To diagnose:

```sh
gh pr view <N> --json statusCheckRollup,baseRefOid,mergeStateStatus
git fetch origin && git rev-parse origin/main
```

- **`mergeStateStatus` isn't CLEAN**: usually `BEHIND` (need to rebase) or `BLOCKED` (missing review approval)
- **`baseRefOid` doesn't match `origin/main`**: main has moved since the PR opened. Rebase.
- **`statusCheckRollup` has a `FAILURE` or an in-progress check**: wait or fix the failing check

GitHub's mergeability state is cached. Bureau independently checks actual head checks and current base state, and repeats the gates just before merging. Background review of a stacked PR uses its actual PR base; that does not weaken the separate merge gate.

The stage tells its caller which kind of refusal it was: exit `2` when the gate is not yet decided (a check still running or not started, GitHub still computing `mergeStateStatus`, a gate read that failed, a hold label `wip`, `blocked` or `needs-human` on the PR, conflicts that the rebase stage resolves because `agents.rebase` is on and the divergence is bureau-only; under the shepherd such conflicts are blocked at once, because the queue's rebase stage skips a ticket the shepherd holds and the shepherd runs only the merge stage at Merge) and exit `25` when it is decided against the merge (a failing check, other conflicts, a stale base, no APPROVE verdict, unresolved threads, a PR that is not open). The gate comment on the PR names the outcome and is posted again only when the outcome or a blocker changes. A hold label counts as "not yet" on purpose: a human put it there and removes it when ready, so the queue does not alert on it every hour; the shepherd waits its budget and then halts with the gate report. A check that no runner takes is "not yet" only for `agents.merge_ci_queued_grace_seconds` (default 3600) and blocked after it (see [A check stays queued](#a-check-stays-queued-runner-offline)). The queue loop stays quiet on `2` and alerts on `25`, at most once an hour per ticket. The merge stage re-evaluates when it next picks the ticket, so re-running a flaky check is enough for it to merge; while other tickets wait, a ticket whose gate was not yet decided or blocked is picked after them for a while (see [A ticket waiting on its merge gate is picked after the others](#a-ticket-waiting-on-its-merge-gate-is-picked-after-the-others)). The shepherd waits on `2` (every `BUREAU_SHEPHERD_MERGE_POLL_SECONDS`, 60 s by default, for at most `BUREAU_SHEPHERD_MERGE_WAIT_SECONDS`, 30 minutes by default) and halts on `25` or on a wait that ran out with `needs-human` and a ticket comment listing the blockers. After fixing the blocker or re-running the check, remove `needs-human` and re-shepherd; the merge stage checks every gate again.

### Doctor warns: the merge gate needs checks, but no workflow runs on pull requests or on pushes to every branch

`agents.merge_require_green_ci` (default `true`) lets a PR merge only when at least `agents.merge_min_required_checks` (default 1) check runs or statuses on its head have completed green. In a repository where no GitHub Actions workflow runs for a pull request's head commit and no other CI reports to GitHub, that never happens: every automatic merge, inline after review or in the merge stage, waits on "only 0 completed check(s)" until the head commit is older than `agents.merge_ci_start_grace_seconds` (default 1800), and is then blocked with "no check run and no status" (`25`, alert, `needs-human` from the review stage). Doctor warns about this when merging is automatic (`agents.merge_mode` `auto` with the review or merge stage on) and no file under `.github/workflows` names a `pull_request` or `pull_request_target` trigger in its `on:` block, or a `push` trigger that runs for every branch: the gate counts check runs on the head commit whatever event started them, so a push workflow on the pull request's branch counts too. A push trigger limited by `branches` (doctor cannot know the pull request's branch name) or to tags still gets the warning, which then names those files; it is harmless when the filter takes in your pull request branches. Fix one of three ways: add a workflow that runs on pull requests (`python3 "$BUREAU_SOURCE/scripts/bureau_install.py" assets --repo "$PWD" --scope ci --apply` scaffolds one, or `/bureau-init --resync-ci`), set `agents.merge_require_green_ci` to `false` for a repository that really has no CI, or set `agents.merge_mode` to `manual`. If another CI (an app or an external status) does report checks to GitHub, the warning is harmless. Only a JSON boolean switches the gate: doctor also warns when `agents.merge_require_green_ci` or `agents.merge_require_up_to_date` holds another value, such as the string `"false"`, which counts as `true`.

### A check stays queued: runner offline

The gate line `ci: check <name> queued for N s on <head>, past the CI queue grace (agents.merge_ci_queued_grace_seconds: 3600) — runner offline?` means a check run on the PR head was created but no runner has taken it for longer than the grace: a self-hosted runner that is offline or stopped, or a `runs-on` label no runner carries. GitHub itself cancels such a job only after 24 hours. A job that GitHub refuses to start for billing reasons (no Actions minutes, a spending limit) is not queued: it fails at once ("The job was not started because recent account payments have failed…") and shows as a failing check. The gate is blocked (`25`): the merge stage's gate comment says so and the queue alerts, at most once an hour per ticket; after an approval the review stage sets `needs-human` and puts the line on the ticket; the shepherd halts. Before v3.2 the check counted as pending for ever and the ticket waited without an alert.

What to do: look at the job (`gh pr checks <N>`, or the run's page in GitHub) and at the runner (`gh api repos/OWNER/REPO/actions/runners`, or on the runner host its service and `_diag` log). Bring the runner back, or cancel the run and start it again (`gh run cancel <run-id>`, `gh run rerun <run-id>`), or fix the `runs-on` label. Once the check runs, the next merge-stage tick finds it pending (not yet) and merges when it passes; at Build Review remove `needs-human` and the next review run reuses the recorded approval without a model call. The time is the check run's `started_at`, which GitHub sets when it queues the job; a check without a readable time stays pending. A busy single runner that works through other jobs first can also cross the grace: on a single self-hosted runner, measure how long jobs wait to start (the gap between `created_at` and `started_at` in `gh api repos/OWNER/REPO/actions/runs/<run-id>/jobs`) before keeping the default, and raise `agents.merge_ci_queued_grace_seconds` above the longest normal wait. A runner outage longer than the grace blocks every approved PR waiting on it, and with the inline merge each of those tickets gets `needs-human`. Only the status `queued` counts; a check waiting for a deployment approval (`waiting`) or for its concurrency group (`pending`) stays "not yet".

### A ticket waiting on its merge gate is picked after the others

A merge gate can stay undecided for a while (checks still running or queued, GitHub still computing) or be blocked without `needs-human` at the merge stage (a failing check, a stale base). Before v3.2 the picker handed out such a ticket on every poll, and the other tickets of its state waited behind it. Since v3.2 it goes after every other ticket for a while:

- **Build Review** (merge agent off): the review stage approved, its inline merge found the gate not yet decided; it recorded the APPROVE, commented once ("not merged yet: its merge gate is not decided") and ended with `2`.
- **Merge** (`agents.merge` on): the merge stage ended with `2` (not yet) or `25` (blocked) and marked the ticket in `$(git rev-parse --git-common-dir)/bureau/merge-gate-waits.json`. The rebase stage is never held.

The queue log says, on every pick in that time:

```text
pick: TEAM-123 waits on its merge gate (not yet) at the unchanged head 1a2b3c4d5e6f — after every other ticket for 2400 s more (1 time(s) in a row)
```

The ticket is still taken when no other ticket of the stage can be picked (`pick: TEAM-123 taken although it waits on its merge gate — no other ticket of the stage can be picked`), so a hold never delays a merge while nothing else waits. The hold lasts two poll intervals after the first "not yet" (at least 300 s; the queue loop's own interval, else `agents.poll_interval_minutes`), doubles with each further one at the same head, and never exceeds `agents.merge_gate_recheck_seconds` (default 3600). Two intervals, because the queue loop sleeps a whole interval after its poll: a hold of one interval has always run out at the next pick. With the default 30-minute poll and other tickets waiting, the ticket is passed over at the next poll and checked again at the one after, so about once an hour; alone, on every poll. With a 5-minute poll the holds are 600, 1200, 2400 and then 3600 s, so it is checked again after about 2, 4, 8 and then every 12 polls while others wait. With a poll of an hour or longer the default cap is shorter than two intervals and nothing is held: set `agents.merge_gate_recheck_seconds` to at least two intervals there.

The bounded tick (`bureau-tick.sh`, one stage per tick) does not spend a tick on such a ticket while another stage has work (v3.2). With `--allow-merge`, when the only ticket it can pick in Build Review or Merge waits on its merge gate, the tick passes over that stage and goes on with the next one:

```text
tick: TEAM-123 waits on its merge gate and is the only code_review ticket that can be picked — passed over while another stage has work
```

No later stage of that tick picks the ticket either (the rebase stage shares the Merge picker). The tick comes back to the stages it passed over, in their order, only when no other stage has a ticket to run (`tick: no other stage has work — back to code_review`, then the picker's `taken although` line when the ticket is still there and still held), and once the hold has run out the ticket is picked in its stage's turn again. While the usage throttle stops the stage that has work, the tick ends there (result `exit_code` 23) and does not come back: a Merge ticket passed over then has its gate checked again only once its hold has run out. `logs/bureau-tick.json` names the stage and ticket that ran, with the usual outcome and exit code; nothing is written for the stage passed over. With `--stage code_review` or `--stage merge` there is no other stage, and the ticket is taken as before. The default `--no-merge` tick is unchanged: it runs no merge stage and skips an approved review at its unchanged PR head and base (`skipped_reviews` in the result). Before, an `--allow-merge` tick took the waiting ticket on every tick, and the stages after it did not run until its gate was decided.

A push to the branch, a branch gone from origin and a record time in the future (a clock that stepped back) end the hold at once. At Build Review the run after the hold reuses the approval (no model call, no new comment) and does not run the build check again when it passed for the same head, base and command with the same `repo.untrusted_env` and `repo.worktree_links` (`Build check not run again: … passed for head …` in the stage output); it runs the gate and merges once it passes. A merge clears the merge stage's mark.

To check a ticket now, run its stage for it (`bash scripts/code-review-pipeline.sh TEAM-123`, `bash scripts/merge-pipeline.sh TEAM-123`) or shepherd it; none of them asks the picker. To switch the holds off, set `agents.merge_gate_recheck_seconds` to `0`. The review record lives in `$(git rev-parse --git-common-dir)/bureau/review-stops.json` (`python3 scripts/bureau-supervision.py resume TEAM-123` removes it, and the next run is a full review), the merge mark in `merge-gate-waits.json` next to it (`python3 scripts/bureau-supervision.py merge-wait TEAM-123 --clear`). When a record or origin cannot be read, the picker holds nothing back and says so in one line (`pick: WARN: the merge gate waits could not be read (…)`). When the gate cannot pass by itself (a hold label on the PR, a required human review; a check that never starts is covered by the queue grace above), the ticket keeps being checked about once an hour while others wait, until a human acts.

### Doctor reports a `repo.worktree_links` directory that holds a `.env` file

`repo.worktree_links entry "settings" is a directory that holds a .env file (settings/.env)` means the stages skip that link: before they link a directory they search it for a `.env*` name in any case, links followed and without a depth limit (`find -L DIR -mindepth 1 -iname '.env*'`), because the link would put the main checkout's secrets into every stage worktree, where pull-request code runs. Doctor runs the same search and names the first hit; its status for the entry is `holds an env file`. Move the file out of the directory, or remove the entry and let `repo.test_command` or `scripts/bureau-test.sh` create what the tests need. A virtualenv with a `.env*` name anywhere inside (a package's example file, say) is skipped the same way. `could not be searched completely` (status `not searched completely`) means the search failed, usually on an unreadable subdirectory (with GNU find also on a link loop, which the BSD find of macOS skips); the stages skip such a directory too, since what the search did not see can hold a `.env`. Make it readable, or remove the entry. Doctor before v3.2 reported both as `ok` while the stages skipped them. The search goes by names: a link under another name that points at a `.env` file is not found, so do not keep such a link in a linked directory.

### Doctor warns `repo.test_command is missing; required for Codex background implementation`

Implement on Codex runs `repo.test_command` itself once the agent reports the work complete, as a check independent of the agent, and stops with `24` when none is set (absent, `null`, `false` or `""`). The warning appears only when implement resolves to Codex: `BUREAU_RUNNER_IMPLEMENT` as the stage sees it (the value in the `.env` the stages read, `BUREAU_ENV_FILE` or else the one next to `.bureau.json`, a relative `BUREAU_ENV_FILE` counted from the directory of `.bureau.json`, never a `.env` of the checkout doctor runs in; doctor reads it with the stages' own reader and never executes it; else doctor's environment), else `agents.implement.runner` when `agents.implement` is an object, else `agents.runner`. It appears with `agents.implement` off as well, because the shepherd runs every stage unless `--respect-config`. Set `repo.test_command` to the project's real test command. Doctor before v3.2 warned on every installation, Claude-only ones included.

### Code-review hit `BUREAU_MAX_REVIEW_CYCLES` — what now?

`agents.max_review_cycles` (default 3) caps how many `REQUEST_CHANGES → Build → Build Review` round-trips before parking.

- Read the last review comment on the PR — it lists what the reviewer keeps flagging
- Resolve substantive findings or explicitly adjudicate disagreements; preserve the required CI/base gates
- If changing the reviewer, use the compatibility-aware Models update flow and confirm the effective provider/model

The background pipeline counts matching Changes Requested comments in Linear. Restarting it does not remove that history. The cap is the last verdict rule, so it also stops rework that only a red build produced: when the reviewers approve and the build check stays red, the review says so ("only the build check stayed red") and escalates at the cap instead of sending the ticket round again. If the comments cannot be read, the stage stops before the paid review (10 or 27) instead of counting cycle 0.

### Why a review ended BLOCKED

The review stage decides its verdict in one order (`decide_review_verdict` in `scripts/bureau-config.sh`), and each rule that fired is appended to the review text:

1. A verdict other than APPROVE, REQUEST_CHANGES or BLOCK is BLOCK ("VERDICT UNREADABLE"). When the merger dropped its json verdict, the text form `REVIEW_VERDICT: X` counts only if X is exactly one verdict word; "NOT_APPROVED — BLOCK" or "APPROVE (with notes)" is BLOCK.
2. The merged review's `security_issues` must be a count (at most nine digits). Missing, negative, implausibly large or anything else is BLOCK ("SECURITY COUNT UNREADABLE"); it is never read as 0.
3. A CRITICAL count above 0 in the security specialist's own json block is BLOCK, whatever verdict the merger chose ("SECURITY: … CRITICAL"). It is read from the whole review, before the merge prompt trims long reviews. If that count cannot be read, the review says so and the verdict is left alone.
4. Any security finding means never APPROVE: APPROVE becomes REQUEST_CHANGES ("SECURITY FLOOR"). A non-critical security bug goes into rework like any other bug.
5. A build that is not green folds the verdict: APPROVE and REQUEST_CHANGES become REQUEST_CHANGES, BLOCK stays BLOCK ("BUILD FAILURE"). The text says the pipeline cannot tell a failure caused by the code from one caused by the environment, and after an approval that only the build check failed.
6. The cycle cap last: a REQUEST_CHANGES at or past `agents.max_review_cycles` is BLOCK ("ESCALATED").

A BLOCK labels `needs-human` and ends the stage with 25; the escalation log names the rule that caused it, or the merger's own BLOCK. Remove the label once the cause is dealt with.

To send the ticket back for the fixes, remove the label and restart it with `scripts/shepherd.sh --from-stage build <ISSUE>`. Since v3.2 implement reads the newest comment that carries findings for it, whichever stage or person wrote it: a Changes Requested review, a BLOCK (`🚫 Code review **BLOCKED** — needs human review.`), QA RED (`🔄 QA: tests failing — routing back to Build.`), QA NEEDS_HUMAN (`🚫 QA flagged for human review.`), a `VERDICT: REQUEST_CHANGES` or `VERDICT: BLOCK` line from the app runtime, or a `FIXES_NEEDED` comment; the three headings count only at the start of a comment, and comments a v3.1 stage posted count too. So a `FIXES_NEEDED` comment written after the BLOCK (your own list, for example one without a finding you overrule) takes precedence; before v3.2 the BLOCK, QA RED, QA NEEDS_HUMAN and app-runtime `VERDICT: BLOCK` comments were not read at all, and the build stage ran without their findings unless someone had written such a comment. Only another finding replaces one: an approval, a passed QA or a halt comment does not, so a build pass that follows one of them (a restart from build, or a stage that moves the ticket to Build without a finding, such as review or QA after a merge conflict with the base or a missing branch marker or PR) is still told to address the older finding; to run a build pass from another list, or with nothing to fix, first write a `FIXES_NEEDED` comment that says so.

### Review comment says `**Build**: not checked`

The review build check found nothing to run: no `repo.test_command`, no `scripts/bureau-test.sh` and no `package.json`. Unlike the QA stage, review does not fall back to `npm test`, `cargo test`, `pytest` or `go test`. The verdict is left as the reviewers gave it, and stderr carries a warning. Set `repo.test_command` in `.bureau.json` to the project's real check so a red build can reach the verdict. When it is set, it wins over the shim and over `npm run build`; a red command (a failure anywhere in a pipe counts, as in QA) turns an APPROVE into REQUEST_CHANGES and never softens a BLOCK. The last 20 lines of the check are in the stage output. The full output, `build.log` in the review's temporary directory, is kept only when the stage exits non-zero; a red check under APPROVE ends in REQUEST_CHANGES with exit 0, and the directory is removed.

The check runs in the review worktree. Files it leaves or changes are named on stderr, split in two. New files git does not ignore (test reports, coverage, bytecode) belong in `.gitignore`. Tracked files the check changed cannot be ignored; the check must stop writing to them. Either way, a stopped or failed review with a dirty worktree keeps it as unfinished work, and the next reset of that worker refuses with exit 21.

### QA returned `NEEDS_HUMAN` verdict

QA parks a ticket with `needs-human` when it can't decide whether a failure is legitimate (test genuinely fails) or spurious (env issue, flaky test, missing service).

```sh
# See what QA saw
grep "$ISSUE" logs/events.jsonl | jq -r 'select(.event=="qa_verdict")'
```

Typically: pull the branch locally, run the tests yourself, and either fix the code or mark the test as skip/ignore with a rationale.

To have the implement stage fix a bug QA found, remove the label and restart with `scripts/shepherd.sh --from-stage build <ISSUE>`: since v3.2 the implement prompt carries QA's summary from the NEEDS_HUMAN comment, as it does on every QA RED, which sends the ticket back to Build by itself. A newer finding takes its place (a review's Changes Requested or BLOCK, another QA RED or NEEDS_HUMAN, a `FIXES_NEEDED` comment); an approval or a passed QA does not (see "Why a review ended BLOCKED").

### Codex-stage-runner failed spuriously

Current pipelines use `bureau-provider.py`; the old `codex-stage-runner.sh` command-string wrapper is a compatibility entry point. Inspect `logs/provider-runs/RUN/`, the resolved provider settings and exit class. Codex implementation/QA are supported through the shell executor, which handles Git publication and independent `repo.test_command` execution. A sandbox denial or missing service is an environment blocker (24), not proof that every write/test stage must use Claude. Resync the coherent runtime if an old unsafe-stage warning remains, then qualify the required capabilities; do not disable sandboxing or silently switch providers.

## Operational

### Agents pause and don't restart — session throttle triggered

Inspect the selected provider's usage signal and `session.usage_threshold_pct` (default 80). Claude can use `BUREAU_USAGE_FILE` or its legacy sources; Codex uses `BUREAU_CODEX_USAGE_FILE` or a shared file tagged `"provider":"codex"`. Numeric `updated_epoch` and `reset_epoch` fields use Unix seconds.

Missing signals allow dispatch. With `pause_on_stale_data: false`, signals older than five minutes are ignored; true continues applying the threshold to their reported percentage. A bounded tick returns until a later invocation instead of sleeping. `BUREAU_DISABLE_THROTTLE=1` deliberately bypasses only this guard. Also inspect the separate `bureau-runtime.py pause` marker before assuming quota is the cause.

### Telegram alerts: once an hour per repository

`alert_telegram` sends a given issue, pipeline and exit code at most once an hour from each repository; the message names the repository (`Repo:`, the directory name of its main checkout). The throttle log is `<git common dir>/bureau/alert-throttle.log` of the repository that holds `.bureau.json`, shared by its worktrees; delete a line or the file to let an alert through again. Before v3.1 every installation on the host shared `/tmp/bureau-alerts.log`, so one repository's alert could silence another's for an hour (a ticket number both teams use, or `none` for a failed pick). Right after the upgrade the new log is empty: an alert sent in the hour before may come once more. The old `/tmp/bureau-alerts.log` is no longer read once every installation on the host runs v3.1; it can then be deleted.

### A `pre-push` hook no longer runs when Bureau pushes

Since v3.2 Bureau's own git commands that talk to a remote (push, fetch, pull, ls-remote, clone, remote, submodule) run with `-c core.hooksPath=/dev/null`: they keep the GitHub token variables, and a hook from the branch would see them. A `pre-push` hook (husky, lefthook, the pre-commit framework's `pre-push` stage) that ran lint or tests on Bureau's pushes therefore no longer runs, nor does a `reference-transaction` hook on a fetch or a push; with git 2.54 and later the same holds for hooks the configuration defines (`hook.<name>.command` and `hook.<name>.event`, which `core.hooksPath` does not reach): Bureau also passes `hook.<event>.enabled=false` for every hook event and `hook.<name>.enabled=false` for every such hook name; hooks of local commands (`pre-commit`, `commit-msg`, `post-checkout`) still run, without the Bureau secrets. Let CI run what the `pre-push` hook checked, or set `repo.remote_git_runs_hooks` to `true` (the JSON value) to run the hooks again with the GitHub tokens in their environment, as in v3.1. A repository that uses Git LFS needs `true`: LFS uploads its objects in the `pre-push` hook, so without it the pull request's LFS files point at objects the remote does not have; doctor warns when the main checkout uses `filter=lfs` (in its `.gitattributes`, a `.gitattributes` further down that git lists, or `.git/info/attributes`) and the key is not `true` ([configuration](configuration.md), [SECURITY.md](../SECURITY.md#code-from-the-branch-and-bureau-secrets)). Doctor warns when the value is not a JSON boolean; any value other than `true` keeps the hooks off.

### Tracing a stage with `bash -x`

Tracing a stage, a driver or the queue loop (`bash -x scripts/…`, or `set -x` and `export SHELLOPTS` in the shell that starts Bureau) is supported. Since v3.2 Bureau looks at and copies `LINEAR_API_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` with the trace switched off (`bureau_secret_set`, `bureau_secret_copy` in `scripts/bureau-env.sh`), so the trace, which the queue loop writes to its log, shows no key; before, `+ API_KEY=lin_api_…` and the presence checks printed it. A script of an installation that a stage starts and that handles a key must do the same. `set -a` with an exported `SHELLOPTS` is switched off in every Bureau script, so it no longer exports the stages' copy of the key.

### A command a stage starts no longer finds `LINEAR_API_KEY`

Since v3.2 the stages and drivers read `LINEAR_API_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` from `.env` into shell variables they never export, so the processes they start do not see them. A Bureau script reads `.env` itself (`bureau_load_env` in `scripts/bureau-env.sh`); a script of an installation that a stage starts and that needs one of the keys must do the same. A key that only the shell environment holds (not in `.env`) is still exported; move it into `.env` instead.

### Worktree collision — "branch already checked out at another worktree"

Git refuses to attach a branch already held elsewhere. `free_branch_from_other_worktrees` reports the holder and does not detach it; when the worker's reset hits it, the ticket gets `needs-human` and a comment naming the holder (see [Exit 21](#exit-21-ownership-conflict--needs-human-and-one-comment)). Inspect `git worktree list` and `python3 scripts/bureau-runtime.py status`, contact the owner and arrange a handoff. An old-looking path is not proof of abandoned work. Only remove a checkout after its owner, saved work and interrupted processes have been reconciled.

### Rebase-pipeline aborted mid-rebase

The pipeline attempts `git rebase origin/main` only for Bureau-owned divergence. On conflict it calls `git rebase --abort` and requests human attention, so a completed failure normally leaves no rebase in progress. Inspect the exact worker with `git status` first. Preserve its HEAD and changes, then perform the appropriate manual reconciliation; use `git rebase --continue` only if Git actually reports an active rebase. Resolve the blocker before removing the human label and resuming.

### Using `BUREAU_DRY_RUN` to debug a specific stage

Use a queue preview for one stage or a named-ticket shepherd preview:

```sh
BUREAU_DRY_RUN=1 bash scripts/queue-loop.sh code-review 1
BUREAU_DRY_RUN=1 bash scripts/shepherd.sh --no-tmux --no-merge TEAM-123
```

The queue example continues polling until stopped; `1` is its interval in minutes. Previews can read Linear, but do not invoke creative work or reset workers. Calling a background pipeline directly inside an app checkout can correctly return ownership conflict 21 instead of performing a preview. Use the app prepare/finish protocol for current-task work.

## Token-efficiency layers

### Implement parks `status=STUCK` on a ticket that's actually complete

Inspect the current result, task marks and branch commits before changing the loop. The portable loop treats an unproductive first `PARTIAL` iteration differently from a valid `COMPLETE` result; it also rejects an empty completion without branch commits. Older installations had different stuck-detection rules. Update source and resync scripts coherently before diagnosing an obsolete path. Claude `/goal` is optional and does not replace Git/test evidence; Codex retains the bounded loop.

### `headroom: command not found` after `headroom_wrap: true`
The flag is read live; turning it on doesn't auto-install Headroom. Install on the host running the pipeline (typically the operator's local box, not the CI runner):

```sh
pip install "headroom-ai[all]"
headroom --version    # confirm
```

If `pip` resolves to a Python that doesn't have user-level installs on PATH, prefer `pip install --user` and add `~/.local/bin` to PATH, or use a venv. The provider adapter checks for the wrapper and reports a missing executable.

### Caveman style leaked into a commit message or PR body
By design, caveman is scoped to per-stage review prose only — commit messages and PR titles/bodies stay in normal register. If you see telegraphic caveman-speak in commits or PRs, the leak path is via the per-stage prompt prefixes in `code-review-pipeline.sh` getting applied where they shouldn't.

Audit: `grep -n "caveman\|/caveman" templates/scripts/code-review-pipeline.sh templates/scripts/merge-pipeline.sh`. The prefix should only appear in review-prose construction sites. If it's in a commit-message or PR-body builder, that's the bug — file an upstream ticket and pin `caveman_level: "off"` until fixed.

### `/goal` is a no-op (turn loops forever or exits without evaluation)

Verify that the selected Claude installation supports `/goal`. The opt-in path does not automatically detect every unsupported CLI behavior. Disable `agents.use_goal_loop` to use the portable bounded loop, or qualify a compatible Claude installation before enabling it. The adapter applies the total timeout; Codex ignores this Claude-only flag.

## tmux

### `WARNING: Old 'bureau' session is still running`
The launcher found a legacy session named `bureau`. Inspect it and its workers before starting another dispatcher; do not delete the warning or kill a session whose owner/work is unknown.

### tmux session already exists on startup

The launcher recreates its own named session. Before rerunning it, pause dispatch and inspect active workers; killing or recreating a tmux session alone does not prove nested provider processes stopped. Preserve unfinished work and runtime leases until inspection confirms a safe handoff. Use `tmux ls` to identify the exact session rather than terminating unrelated sessions.

### Pane shrunk to 0 rows / "no space for new pane"
Already fixed — `start-bureau-v2.sh` re-tiles after every split. If you see this on an old install, run `/bureau-init --resync-scripts`.

---

## Linear state divergence

### PR is OPEN but Linear says Done
The verify-merge fix in `code-review-pipeline.sh` should prevent this — after `gh pr merge`, the pipeline confirms the actual PR state before claiming Done. If you see this on an old install, resync scripts.

### Issue marked Done but no PR exists
Check the bureau-branch comment marker. If it points at a branch that was deleted before merge, the pipeline may have lost the PR reference. Manually re-open the issue and route through Build Review.

---

## Claude / auth

### Pipeline fails with `claude-unauth` (exit 16)

Exit 16 now covers the selected provider. Inspect `claude auth status --json` or `codex login status` and complete that provider's normal login flow. Bureau checks authentication without a probe generation. This is separate from the Linear API key used for ticket CRUD.

### Headless Claude calls hang or timeout

Inspect the adapter's preserved stdout/stderr and result metadata before retrying; after a Claude timeout `stdout.log` is empty, and the transcript path in `result.json` shows what the call did (see [Exit 124](#exit-124-timeout--a-provider-pass-hit-its-time-limit)). Confirm provider login, network availability and the configured timeout. Exit 124 indicates the bound was reached, 130 cancellation, and 24 an environment/permission failure. The bound is `timeout_seconds` per provider call: 3600 s by default since v3.1 (900 s before), set per stage or per provider in `.bureau.json`; doctor warns when an enabled spec, spec review, UX, QA or review stage gets less than 1800 s. A retry requires inspection of interrupted ownership; avoid an unbounded probe or a permissions bypass as a diagnostic shortcut.

### Speckit phases produce empty `tasks.md`
The spec pipeline routes back to Triage automatically. To debug:

```sh
ls -la specs/<NNN-feature>/
cat logs/queue-spec.log | tail -100
```

Most common cause: the Linear issue description was too thin for `/speckit-tasks` to extract anything. Add acceptance criteria to the description and re-trigger.

---

## Supervisor

### A review says it reused the approval
The ticket comment "reusing the approval recorded …" means the review stage found the approval a `--no-merge` run recorded for the same PR, base branch, head and base commits, ticket text and state, and moved on without a new model review; only the build check ran again. An approval recorded because the merge gate was not yet decided is reused without that comment, and without the build check when it passed for the same command and environment settings (see [A ticket waiting on its merge gate is picked after the others](#a-ticket-waiting-on-its-merge-gate-is-picked-after-the-others)). The record is gone after that. To get a fresh model review at the same head instead, run `python3 scripts/bureau-supervision.py resume TEAM-123` before the next run. A changed head, base, ticket or PR, a record written without a verdict (by a runtime from before this change), a ticket detail that cannot be read, or an unreadable `review-stops.json` always gives the full review; the last one also prints "the review boundary file could not be checked".

The comparison covers the PR and the ticket, not the reviewer: a different model, runner, review prompt, `.bureau.json` or Bureau version between the stop and the resumed run does not by itself cause a new review. If you changed one of those and want the change to judge the PR, run `python3 scripts/bureau-supervision.py resume TEAM-123` (or push a commit) before resuming.

### Telegram alert: "supervisor giving up"
The supervisor crashed `BUREAU_SUPERVISOR_MAX_CRASHES` times in a row. The alert includes the tail of `logs/queue-<mode>.log`. Common causes:

- Bash syntax error introduced by an unfinished script edit
- Missing config key after a partial `/bureau-init --update`
- `LINEAR_API_KEY` revoked

After fixing, restart with `./scripts/start-bureau-v2.sh` or just re-run `./scripts/queue-loop-supervised.sh <mode> <interval>`.

### Crash counter stuck high
Restart the supervisor — counters are in-process, so a fresh launch resets them. Or wait `BUREAU_SUPERVISOR_STABILITY_WINDOW` seconds (default 1 h) of clean runtime for the auto-reset.

---

## Drift / upgrades

### Old commands at `.claude/commands/speckit.*.md`

Older Spec Kit integrations used command files. Refresh Spec Kit separately with the pinned installer and intended targets, preserving the active integration unless a switch is requested. Verify the new `.claude/skills` and/or `.agents/skills` entry points before reviewing obsolete files for removal. Preserve customized command content; do not delete the old glob blindly.

### Resync refuses `CLAUDE.md` or `AGENTS.md`: `legacy Bureau section at line N`

The file still carries a section an older installer generated, opened by `<!-- bureau-init managed -->` or a variant such as `<!-- bureau-init managed: regenerate via … -->`, or closed by `<!-- end bureau-init managed -->`. The installer does not guess where that section ends and does not append a second Bureau block next to it; the whole asset batch writes nothing, and `--overwrite` does not bypass it. Open the named line, decide which lines are the old generated guidance, and put `<!-- bureau-init:begin -->` and `<!-- bureau-init:end -->` in place of the old markers around exactly those lines (or delete the old section). The reason says `outside the bureau-init:begin/end block` when the file already has a new block and the old section sits next to it: remove the old section, or move what is still needed into the block. Then preview again; adopting a hand-delimited block needs `--overwrite CLAUDE.md` once, because its content does not match any recorded install.

### Preview lists `skipped` template files

The installer never installs a template file the source skill checkout ignores (`.DS_Store`, `.env*`, `*.log`, `__pycache__` and whatever else its `.gitignore` names); the preview lists them under `skipped` and on stderr. A tracked file is never skipped. A source that is not its own git checkout skips its dotfiles and whatever its `.gitignore` files match. If a file you expect is skipped, it is ignored in the source: commit it there, or remove the ignore rule.

### Constitution missing after speckit resync

The current installer snapshots and restores an existing constitution, including on a failed Spec Kit invocation; it does not promise a persistent `.bak` file. Stop further resync, inspect the installer error and your pre-upgrade backup, and restore the actual constitution rather than generating a replacement. See [migration and rollback](migration.md).

## See also

- [Configuration](configuration.md) — config keys + env vars
- [Exit codes](exit-codes.md) — what each failure code means
- [Recipes](recipes.md) — common config patterns

## App ownership and permissions

An ownership conflict is not permission to detach another checkout. Inspect `bureau-runtime.py status`, locate the owner, and resume or perform an authorized handoff. `release RUN_ID` refuses a live background owner. App leases do not expire automatically: release only after confirming the old task has stopped. Unfinished workers retain progress and lose their disposable registration so later ticks cannot erase it.

For exit 24, inspect the exact denied operation. Codex leaves Git commits to the shell executor, and the executor independently runs configured implementation/QA tests. App test actions run under app permissions. Grant a needed capability through the supported host flow or report the environment blocker; do not weaken merge gates or report unrun tests as passed.

A prepared stage records the current branch, canonical issue marker, state, config and HEAD. If any change unexpectedly, reconcile the actual branch/ticket and prepare a fresh run. Never rewrite a result's HEAD merely to pass validation.

## Quota and missing cost

Codex does not consume ClaudeWatch signals. Configure a provider-tagged usage signal, or use the app's account-usage tool for interactive supervision. `null`/`unavailable` cost means the CLI supplied no estimate; it does not mean the work was free. A bounded tick waits until a later invocation instead of sleeping through the schedule.
