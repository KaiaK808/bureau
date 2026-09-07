---
name: bureau-init
description: Install or update Bureau's Linear and Spec Kit workflow in a git repository for Claude Code, Codex, or both. Use for Bureau setup, configuration changes, and template resync.
---

# Bureau setup

Bureau installs project instructions, Linear workflow commands, Spec Kit skills and optional background scripts into another repository. Use the current user's requested scope and existing choices; installing a workflow does not authorize starting workers or merging PRs.

Resolve this skill's root from the loaded file location, including symlinks. All template, script and reference paths below are relative to that root. Never assume a particular home directory installation path.

## Usage and routing

Claude invokes `/bureau-init`; Codex invokes `$bureau-init` or requests it in ordinary text. Read arguments from the user's request; no host-specific argument injection is required.

| Request | Reference |
|---|---|
| First setup, `--target claude|codex|both` | [Setup](references/setup.md) |
| `--update` | [Configuration update](references/update.md) |
| `--resync-interfaces`, `--resync-scripts`, `--resync-workflows`, `--resync-speckit` | [Resync](references/resync.md) |
| `/bureau-init --resync-ci` (or Codex equivalent) | [Resync](references/resync.md) |
| Background mode selection, operational knobs or installed-script orientation | [Background operations](references/operations.md) |
| `--help` | Show [README](README.md) or open `docs/site/index.html` from this skill root |

For installation, use `scripts/bureau_install.py`: preview is the default; `--apply` writes reviewed changes. Select Claude, Codex or both independently of the background runner. App setup must not require Claude or tmux. New dual installations need an explicit active Spec Kit integration.

Preserve `.bureau.json`, credentials, user instructions, customized templates and constitutions. Conflicts stop an asset batch before writes. Replacing a customized file requires an explicit named `--overwrite` path after reviewing its diff and the user's authorized scope. Existing authorization is sufficient; do not ask again for changes already requested.

The canonical runtime files are `templates/scripts/`; copy them, never reconstruct them. [Runtime patterns](references/runtime-patterns.md) explains existing background behavior when needed for maintenance. Do not imply that installing the Codex interface alone completes background runner support.
