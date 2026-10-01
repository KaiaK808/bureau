# Background providers

Current app tasks use the app's selected model. Background jobs use `bureau-provider.py` through the `run_stage_for` shell function. The function passes prompts and system context through private files; the adapter builds argv arrays, streams the prompt on stdin, keeps provider diagnostics in separate logs, and returns only the final result. Legacy `claude_cmd_for_stage` remains for compatibility; shipped pipelines no longer execute its command strings.

```json
{
  "version": 2,
  "agents": {
    "runner": "codex",
    "spec": true,
    "implement": true,
    "qa": true,
    "code_review": {"enabled": true, "runner": "claude"},
    "providers": {
      "codex": {"reasoning_effort": "high", "timeout_seconds": 2400}
    },
    "workbench_panes": 0
  },
  "repo": {"test_command": "bash scripts/bureau-test.sh"}
}
```

Runner precedence: `BUREAU_RUNNER_STAGE`, stage runner, default runner, then Claude. Unknown runners are errors. Model precedence: provider-specific stage environment override (`BUREAU_CODEX_MODEL_IMPLEMENT`), generic stage environment override, stage model, provider model, default model when it belongs to the selected default runner, provider environment default, then Claude's legacy environment fallback. With no model setting the provider CLI chooses its configured default. A Claude default does not leak into a Codex stage. Use actual model identifiers available to the account; reasoning settings remain subject to the selected provider/model's support.

That model precedence applies to version 2 configurations. Version 1 (including configurations without a version) preserves the older rule: generic `BUREAU_MODEL_*`, `agents.model` and stage `model` fields belong to Claude and are ignored for Codex. Set Codex models through `BUREAU_CODEX_MODEL_STAGE`, `agents.providers.codex.model` or `BUREAU_CODEX_MODEL_DEFAULT`. A migrated configuration can retain this behavior with `agents.model_compatibility: "v1"`, including after future runner environment overrides. Remove that marker from a version 2 configuration, or explicitly set it to `"v2"`, only after reviewing generic model fields for their selected providers. Other marker values are rejected.

`agents.STAGE.sandbox` or `agents.providers.PROVIDER.sandbox` can select `read-only` or `workspace-write`; `BUREAU_SANDBOX_STAGE` overrides them. Code review and research default to read-only; spec review needs workspace-write because it repairs spec artifacts. Bureau does not turn off the Codex sandbox. Normal app/OS restrictions still apply. Codex leaves Git commits/pushes to the deterministic executor; successful implementation also runs `repo.test_command` outside the nested model sandbox. QA runs the configured command independently too. Permission/environment failures are reported separately from failed project tests.

The adapter itself starts without the Bureau secrets, and the agent process and its login check start without them too: by default without `LINEAR_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`, `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, any variable that carries one of their values, `BASH_ENV` and `ENV`; with `repo.untrusted_env: "clean"` with only the base variables plus the provider's own login and network variables (see the [configuration reference](configuration.md) and [SECURITY.md](../SECURITY.md#code-from-the-branch-and-bureau-secrets)). An invalid `repo.untrusted_env` returns 24 before the login check. Authentication uses `codex login status` or `claude auth status --json` before creative work, without a probe generation. Missing executables/login return 16. Provider errors or malformed final results return 22; quota 23; environment/permission problems 24; timeout 124; cancellation 130. Timeout and cancellation finish by killing the provider process group even if its immediate CLI process has already exited; a surviving descendant cannot keep writing after the bounded invocation returns. Per-stage/provider `timeout_seconds` defaults to 3600 per call (900 before v3.1); doctor warns when an enabled spec, spec review, UX, QA or review stage gets less than 1800 s. Implementation uses its existing iteration/total bounds through `BUREAU_STAGE_TIMEOUT`; since v3.2 a pass that times out is one pass of the loop, and the stage ends with 124 only when no pass is left ([troubleshooting](troubleshooting.md#exit-124-timeout--a-provider-pass-hit-its-time-limit)).

JSON schema contracts cover implementation iterations, QA, and the merged review verdict. Both plain JSON and legacy fenced JSON are parsed; Claude envelopes are unwrapped. A provider's diagnostic stream cannot supply the verdict. Raw prompt/output/error evidence and metadata remain under `logs/provider-runs/RUN/`. `result.json` there also names the provider's own record of the call, which outlives a timeout: since v3.2 every Claude call starts with a fresh `--session-id`, and `session_id`, `transcript` (`<CLAUDE_CONFIG_DIR or ~/.claude>/projects/<slug>/<session id>.jsonl`, the slug being the resolved working directory with every character other than a letter or digit replaced by `-`; when Claude placed it elsewhere under `projects/`, the file found there) and `transcript_found` record it. For Codex, `session_id` is the thread id of the first `thread.started` event in `stdout.log` and `transcript` the matching rollout file under `<CODEX_HOME or ~/.codex>/sessions/`, or `null` with a `transcript_note` when Codex reported no thread id. The stage log carries the path as `Bureau provider transcript: …`. The transcript holds the whole session, tool output included; it stays where the CLI wrote it, outside `logs/`. Optional `session.cost_tracking` preserves the legacy usage envelope interface. Unknown dollar cost stays unavailable for Codex.

Claude `/goal` and Headroom are Claude capabilities. A Codex implementation uses the portable bounded loop even when the old goal flag is enabled. Interactive bench panes use `agents.workbench_runner` or the default runner; zero panes omits the bench. Optional upstream summaries are disabled by default; enabling `agents.upstream_summary` uses a tool-free Claude or read-only Codex pass.

A failed worker with uncommitted or unpublished progress loses its disposable registration. Its checkout remains available for inspection/resume instead of being erased by the next tick. A clean failure can retry normally. See [ownership and resume](stage-protocol.md).

QA, spec review and copy publish newly created files as well as tracked edits through the shell executor. Local credentials/configuration and provider evidence remain outside these commits. Already-staged private files block publication without changing the index; inspect and unstage them before retrying. Commit or push failures stop routing so that the next stage cannot receive an unpublished result.

Validation currently uses fake executors and real local Git/test commands. Run a representative live ticket in an adopting repository before relying on unattended Codex or mixed-provider operation.
