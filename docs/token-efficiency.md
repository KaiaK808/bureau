# Optional token-efficiency settings

These options default off. They affect background execution; current Codex app tasks use the app's selected model, tools and user instructions. No measured Codex savings are claimed by this repository's fixture tests.

| Setting under `agents` | Scope | Behavior |
|---|---|---|
| `use_goal_loop: true` | Claude implementation only | Uses the existing Claude goal prompt path instead of the shell iteration loop |
| `headroom_wrap: true` | Claude provider only | Executes `headroom wrap claude -- ...` with the normal adapter result contract |
| `caveman_level` | Configured review prose | Requests abbreviated prose; normal commit messages and PR descriptions remain readable |

Codex uses the bounded implementation loop even when a legacy goal flag is set. It never receives Claude `/goal` flags or the Headroom wrapper. Provider final-output validation, ownership, state guards and merge gates apply regardless of these settings.

The Claude goal path depends on the installed Claude CLI's goal capability. Check the local version and behavior before enabling it. The outer adapter still enforces its wall-time limit; a model-evaluated completion condition is not proof that tests passed. Inspect the actual result and independent evidence.

Headroom must be installed on the background host before enabling the wrapper. The adapter reports a missing executable instead of silently omitting compression. When compressed context is insufficient, retrieve the original using the available tool; do not infer missing code or error details. See [Headroom](https://github.com/headroomlabs-ai/headroom) for the external project.

Caveman is an optional separate skill. Honor explicit user communication preferences, keep code/paths/errors exact, and retain enough reasoning for a reviewer to assess a finding. See [the skill project](https://github.com/JuliusBrussee/caveman) for installation and supported modes.

Enable one option at a time on representative work, compare the actual provider evidence, and turn it off if quality or reliability worsens. Changes take effect on subsequent invocations. Rollback is setting `use_goal_loop` or `headroom_wrap` to false, or `caveman_level` to `off`; no artifact/state deletion is needed.

`session.cost_tracking` records available token usage and CLI estimates. Missing Codex dollar cost remains unavailable. These records do not show actual billed cost or account quota; the app's account usage tool is separate. See [configuration](configuration.md) and [provider runtime](provider-runtime.md).
