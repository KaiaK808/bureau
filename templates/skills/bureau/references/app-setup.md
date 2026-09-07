# App setup and actions

The helper `bash scripts/bureau-app.sh setup` copies missing ignored Bureau configuration and environment files into a new worktree, retaining existing files. It does not install dependencies or run model processes. Add the target project's usual dependency setup separately.

In the app's local-environment settings, add that setup command and these actions, preserving any existing setup/actions:

| Name | Command |
|---|---|
| Bureau status | `bash scripts/bureau-app.sh status` |
| Bureau doctor | `bash scripts/bureau-app.sh doctor` |
| Bureau validation | `bash scripts/bureau-app.sh check` |
| Project tests | `bash scripts/bureau-app.sh test` |

Set `.repo.test_command` in `.bureau.json` to the actual project test command for the tests action. The command is project configuration and executes under the app's permissions. An absent command is reported rather than guessed.

Use the supported app settings UI to create or extend the local environment; let the app generate its `.codex` configuration. Do not invent a configuration schema or overwrite unrelated `.codex` files. When UI access is unavailable, present the exact commands and remaining settings step. These commands also work directly in the integrated terminal.

See [official local-environment documentation](https://learn.chatgpt.com/docs/environments/local-environment) for the settings, setup and action surfaces. This workflow does not change model settings, approval policies, or scheduling.
