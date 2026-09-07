## Update Mode

When the user requests `--update`:

1. **Show current effective config first.** Run `bash scripts/bureau-status.sh --config` and display the output. This surfaces both `.bureau.json` values and the env-only knobs (`BUREAU_IMPL_*`, `BUREAU_SUPERVISOR_*`, `LINEAR_API_KEY` set/unset, etc.) the user might want to change.

2. Ask: "What would you like to change?" Present these option groups:
   - **Linear** — teams, labels, states (`.bureau.json` → `.linear.*`)
   - **Agents** — toggle on/off (`.bureau.json` → `.agents.<stage>`)
   - **Tuning** — poll interval, max review cycles, concurrency cap, sampling threshold, merge strategy (`.bureau.json` → `.agents.*`)
   - **Models** — provider defaults and per-stage overrides; select fields using the compatibility rules below
   - **Repo** — branch prefix, commit prefix, specs dir (`.bureau.json` → `.repo.*`)
   - **Retry loop** — `BUREAU_IMPL_MAX_ITER`, `BUREAU_IMPL_ITER_TIMEOUT`, `BUREAU_IMPL_TOTAL_TIMEOUT` (`.env`, env-only)
   - **Supervisor** — `supervisor.max_crashes`, `supervisor.stability_window` (`.bureau.json` OR `.env` — env overrides; ask which surface)
   - **Runtime** — `BUREAU_DRY_RUN`, `BUREAU_SESSION_NAME` (`.env`, env-only)

3. **For the selected section, edit the right surface:**
   - JSON-backed values → modify `.bureau.json` in place. Preserve unrelated keys, preserve key ordering where possible.
   - Env-only values → update `.env` inside the bureau-managed block (see below). **Never** delete or reorder unrelated entries (`LINEAR_API_KEY`, `TELEGRAM_BOT_TOKEN`, user-set vars).
   - Model changes → inspect the selected runner and `agents.model_compatibility` first. Version 1 (including an absent version), and migrated configs retaining `model_compatibility: "v1"`, ignore generic model fields for Codex. Preserve those Claude settings. Use `agents.providers.codex.model` for a Codex default, or `BUREAU_CODEX_MODEL_<STAGE>` for a stage-specific Codex override. Provider-specific stage environment overrides also work for Claude. Only v2 model semantics allow `agents.<stage>.model` to select that stage's runner model; changing to those semantics is a separate explicit configuration choice, not a side effect of selecting a model. See [model precedence](../docs/provider-runtime.md).
   - Verify each changed stage with `python3 scripts/bureau-provider.py --stage STAGE --describe` in the same trusted environment used to launch it. Doctor and provider description do not load `.env`; account for its overrides without printing credentials. Confirm the resolved provider/model, and identify higher-priority settings when a changed default has no effect. This read-only check does not prove model access or invoke a model.

4. **The bureau-managed block in `.env`:** Bureau-init manages env-only knobs inside delimited markers so the user can hand-edit secrets and other vars above the block freely.

   ```
   # User-set entries (LINEAR_API_KEY, TELEGRAM_*, etc.) live above this line.

   # ── bureau-init managed (do not edit between markers; run /bureau-init --update instead) ──
   BUREAU_IMPL_MAX_ITER=5
   BUREAU_IMPL_ITER_TIMEOUT=2400
   BUREAU_DRY_RUN=0
   # ── end bureau-init managed ──
   ```

   When updating: locate the markers (or append the block at end of file if absent), rewrite ONLY the lines between them. Drop a line by setting it to its default value (the script reads `${VAR:-default}` style, so the line just exists as documentation in that case — or remove the line entirely; both are equivalent at runtime).

5. **Keep update config-only.** Do not regenerate scripts, interfaces or Spec Kit assets. Scripts read configuration at runtime; asset changes use the explicit resync modes.

6. **Re-run `bash scripts/bureau-status.sh --config`** and show the diff between before/after. Summarize the applied changes and any remaining validation.

**Source legend in the status output:**
- `json` — value comes from `.bureau.json`
- `env *` — env var is set and takes precedence over any `.bureau.json` value
- `def` — value is the in-code default (env unset, no `.bureau.json` key)


For model choices, use the provider's available models or the current configured default. Do not hardcode pricing or assume a Claude model identifier works with Codex.
