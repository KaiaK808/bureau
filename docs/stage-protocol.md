# Shared stage execution and ownership

Bureau supports two workspace modes. App work uses the current checkout through `python3 scripts/bureau-runtime.py`; background scripts run through `bureau-worker.sh` in explicitly registered disposable worktrees. Direct background stage execution in a user checkout is refused. These controls do not bypass Codex permissions for the checkout or shared Git directory.

The shared Git directory stores `bureau/leases.json`, run records and disposable-worker registration. Claims cover both issue ID and canonical checkout path, using an OS file lock around updates. An app and a worker cannot own the same local issue or checkout concurrently. A worker releases its own branch after a stage. Held branches in other worktrees cause exit 21 rather than detaching the owner. Existing `.worktrees/` directories are not implicitly disposable; inspect and retire or hand off an old worker before starting a new registered one.

## App stages

```sh
python3 scripts/bureau-runtime.py status
python3 scripts/bureau-runtime.py prepare TEAM-123 implement --owner app-task
# Follow scripts/bureau-stage.md in the current task and write result.json.
python3 scripts/bureau-runtime.py finish RUN_ID --result result.json
```

The prepare response records issue/state identity, configuration hash, current HEAD, branch, canonical marker and project context. Attach an existing canonical branch before preparing. Finish requires a matching run/stage, live ownership, current result HEAD, unchanged configuration and unchanged issue state. The branch and canonical marker must still agree; review additionally requires the prepared HEAD. Complete spec work needs spec/plan/tasks artifacts in one feature folder under specs_dir; implementation/QA need successful test evidence. Partial/blocked results retain the current Linear state and save their explanation. Blocked outcomes and BLOCK reviews also apply the configured needs-human label before releasing ownership; every background queue excludes that label. A label failure keeps the run pending for retry. Resolve the blocker and explicitly remove its label before resuming background processing. Completion posts the canonical branch marker and a run marker, then moves to the configured state UUID. Repeating the identical result is idempotent, including recovery after a successful state move interrupted before local completion. Once finish records its pending result, retries must use that exact result and target state. Spec-review routing reads current configured labels at finish, including labels added during review. App REQUEST_CHANGES comments are consumed by the background implementation loop.

For resume, inspect `status --run RUN_ID`, the actual branch/diff and current Linear state. Continue unfinished work using its run ID. If the state/configuration/head makes the result stale, preserve the work, release the old run, and prepare a fresh stage after reconciling the change. `release RUN_ID` refuses a live background PID or a recorded live process group. An interrupted background run retains its issue/workspace claims and loses disposable-worker registration, even if its immediate shell already exited. Inspect the saved work and the process-group record at `<shared-git-dir>/bureau/processes/RUN_ID.json`, verify current process identities, stop any remaining writers, then release the run explicitly. The preserved checkout remains unregistered: adopt it for app recovery or explicitly retire it before creating another disposable worker. Process inspection failures and reused live group IDs fail closed; do not kill a process solely because an old record mentions its ID. After a machine crash, inspect the old run before releasing it; app leases have no implicit expiry. This is local coordination, not a distributed lock or an atomic Linear compare-and-swap across hosts.

## Configuration and merge boundaries

`BUREAU_CONFIG` is an explicit path and is honored even from a linked worktree. Otherwise discovery checks the current root, primary checkout, and script installation root. `setup` copies ignored `.bureau.json` and `.env` from the discovered source into a new app worktree only when absent; existing files stay intact. Credentials remain local. Core operations use the direct API-key path for Linear state changes; available Linear tools can still serve interactive discovery.

`BUREAU_NO_MERGE=1` applies to direct merge, review, queue and shepherd paths; queue/shepherd also accept `--no-merge`. Code review exits 20 after approval when the stop is requested. Otherwise inline merging delegates to the same gated merge pipeline used by the Merge worker, including when no Merge state is configured. The app prepare/finish protocol stops after review by default. Its `--allow-merge` option permits routing into a configured Merge queue; it never executes a merge itself.

New shell-generated commits carry `Bureau-Generated: true`. The ownership check still recognizes legacy Claude trailers/spec commits and merge commits. An unresolved Git reference fails the ownership check rather than counting as safe.

`status` is read-only. Shepherd's dry run describes a requested `--from-stage` without executing it and does not authenticate a model, claim an issue, or invoke a stage. Use these inspection paths before starting work.
