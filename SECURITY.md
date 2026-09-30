# Security policy

## Reporting a vulnerability

Do NOT open a public GitHub issue for a security vulnerability. Public issues are indexed and searchable immediately.

Instead, email the maintainer directly:

- **Kai Ebert** — 808.meets.303@gmail.com

Include:
- A clear description of the vulnerability
- Steps to reproduce (a minimal repro, ideally against a fresh `/bureau-init` install)
- What you believe the impact is (e.g., "an operator running `/bureau-init` in a repo controlled by another user could exfiltrate the operator's `.env`")
- Any suggested fix, if you have one

You should get an initial acknowledgement within a few days. If the vulnerability is confirmed, a fix will land on `main` and the reporter is credited in the release note unless they prefer anonymity.

## Supported versions

Only `main` is supported. Bureau doesn't cut tagged releases; every merged PR ships to every installed clone the next time an operator runs `git pull` in `~/.claude/skills/bureau-init/` or `/bureau-init --resync-scripts` in a target repo.

If you're pinned to an older SHA, upgrading to current `main` is the fix. There's no LTS branch.

## Scope

**In scope:**
- The templates in `templates/scripts/` (the shell scripts installed into target repos)
- The `/bureau-init` skill logic in `SKILL.md`
- The upstream-port fast-path in `templates/scripts/upstream-port.sh`
- The `docs/site/` HTML docs

**Out of scope:**
- Vulnerabilities in target repos' own code (that's the repo owner's responsibility)
- Vulnerabilities in Claude Code itself (report to Anthropic)
- Vulnerabilities in Linear, GitHub, or the `gh` CLI (report to the respective vendors)
- Vulnerabilities in [caveman](https://github.com/JuliusBrussee/caveman), [Headroom](https://github.com/headroomlabs-ai/headroom), or [speckit](https://github.com/github/spec-kit) — those are third-party dependencies with their own security policies

## Code from the branch and Bureau secrets

Bureau runs code a branch controls before any human has reviewed it: the review build check (`repo.test_command`, `scripts/bureau-test.sh` or `npm run build`), the three QA test runs, `repo.post_implement_command`, the Codex completion test, the app `test` action, the build and test commands of `upstream-port.sh`, and the agent processes, which run the branch's tests themselves (Claude with `--dangerously-skip-permissions`) and load the branch's own agent settings and hooks. Since v3.1 each of them starts in a reduced environment (`bureau_untrusted_env` in `scripts/bureau-env.sh`, `untrusted_env` in `scripts/bureau-provider.py`). By default it lacks `LINEAR_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`, `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN`, every other variable that carries one of their values (a value of 6 characters or more anywhere inside, such as the stage's `API_KEY`, a remote URL with a token in it or a copied `Authorization` header; a shorter one only as the whole value, because matching two to five characters inside other values would remove variables that merely contain them), and `BASH_ENV` and `ENV`, which would make a bash child source a file such as `.env` before the branch's command runs. With `repo.untrusted_env: "clean"` it holds only a short list of base variables (see the [configuration reference](docs/configuration.md)). The stage itself keeps the keys for its own Linear, GitHub and Telegram calls.

Branch code also gets in through Bureau's own work, and there the keys are kept out as well:

- **Git hooks and filters.** Bureau's own git commands in a stage (checkout, add, commit, merge, rebase, reset, cherry-pick and the rest) run the repository's hooks and filters, and those can come from the branch: `core.hooksPath` pointing at a tracked directory, the pre-commit framework's `.pre-commit-config.yaml`, `lefthook.yml`, husky v4's `package.json`, a filter that `.gitattributes` picks. Every git command in the stage scripts goes through a `git` function (`scripts/bureau-env.sh`) that runs it without the seven variables, their copies, `BASH_ENV` and `ENV`; the git and `gh` processes Bureau's Python starts (`bureau-supervision.py`, `bureau-runtime.py`, `bureau-doctor.py`, through `process_env` in `bureau-provider.py`) get the same environment, so an fsmonitor hook or a credential helper they run sees no key either; `gh`, which runs git itself, goes through a `gh` function that removes the `.env` keys and keeps the GitHub token variables it logs in with. Hooks and filters keep running; Bureau does not skip them.
- **Python modules in the branch root.** Inline Python in the scripts runs with `python3 -I`, so a `subprocess.py`, `pathlib.py` or `hashlib.py` the branch commits cannot stand in for the standard library.
- **Bureau's own processes.** `bureau-provider.py`, the agent's parent, starts without the seven. The runtime wrapper `bureau-runtime.py`, an ancestor of every stage, starts without `BASH_ENV` and `ENV` — the worker changes into the branch's worktree before it starts the stage's bash, so a relative `BASH_ENV` would name a file of the branch — and without the `.env` keys that the stage it relaunches reads back from `.env`. The Linear and Telegram requests give the key, the bot token, the chat id and the alert text (whose log tail can hold whatever a failing tool printed) to `curl` on stdin (`curl -K -`), never in its argument list, which `ps` shows to other processes and on Linux to other users, and `curl` starts without the seven. The helpers call `env` as `/usr/bin/env`, so a PATH entry such as a branch's `node_modules/.bin` cannot stand in for it.

This closes the accidental paths — a dependency or build step that reads, logs, uploads or bundles its environment, a test that prints its environment into a log that ends up in Linear or on a pull request — and the ways in listed above. Code that deliberately looks for the keys while running as the same Unix user as Bureau can still find them. What stays open, stated so nobody relies on more:

- **Files.** The main checkout's `.env` (the file the stages read, `BUREAU_ENV_FILE`) and `.bureau.json` are readable by the same user. Stage worktrees hold no `.env` after their reset (`git clean -fdx`), and the stages skip a `repo.worktree_links` entry that is a `.env*` file (any path component starting with `.env` in any case, or an entry whose target in the main checkout is such a file) with one warning line; `bureau-doctor.py` reports such an entry as an error. The stages also skip a directory with a `.env*` name anywhere below it.
- **Stored logins.** Removing `GH_TOKEN` and `GITHUB_TOKEN` does not remove `gh`'s own login: `gh auth token` prints the token stored in `~/.config/gh/hosts.yml` or the system keychain, and a git credential helper (`git credential fill`, the macOS keychain helper) hands out the GitHub credential the same way. The same holds for any tool that keeps its login under `~/.config` or in the keychain.
- **Git commands that talk to a remote.** `push`, `fetch`, `pull`, `ls-remote`, `clone`, `remote` and `submodule` keep `GH_TOKEN`, `GITHUB_TOKEN` and the enterprise variants, which a credential helper may read; they lose only the `.env` keys. A hook from the branch that runs during them therefore sees the GitHub tokens: `pre-push` on a push, `reference-transaction` on a fetch or a push; so does a credential helper. `gh` keeps them too.
- **Environments of other processes.** A process's environment as it was started can be read by the same user: on Linux through `/proc/<pid>/environ`; on macOS through `ps -E`, checked on macOS 26 for Homebrew's `python3` and for `node`, while `/bin/bash`, `/bin/zsh`, `/bin/sleep` and `/usr/bin/jq` showed nothing. After this change the ancestors of branch code start without the `.env` keys (the runtime wrapper, the stage and worker shells it relaunches, the provider), except a driver started from a shell that exported them itself. The GitHub token variables from the operator's shell stay in the runtime and the stage shells, because the stages' `gh` calls use them. Not ancestors, but readable while they run: the processes a stage starts after it has read `.env` and exported the keys — its `gh` and `jq` calls and its Python helpers — hold them. A branch process left running in the background (a test that starts a server) can read those on Linux, and on macOS where the executable is not an Apple platform binary (a Homebrew `gh` or `python3`). Closing this needs the stages to stop exporting the keys.
- **Everything else in the default environment.** The default removes only the Bureau-owned secrets and the GitHub token variables. An SSH agent (`SSH_AUTH_SOCK`), cloud credentials and registry tokens from the operator's environment stay; `repo.untrusted_env: "clean"` drops them.
- **The agent's own login.** `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY`, when the agent logs in through them, reach what the agent runs: the agent cannot run without its login.
- **Relative PATH entries.** `python3`, `git`, `jq`, `gh` and `curl` are found through PATH; a relative entry such as `node_modules/.bin` in the operator's PATH would let the branch put its own program there. Keep PATH free of relative entries on a Bureau host.

Not a path: the dependency restore after a worktree reset runs `npm ci --ignore-scripts` on copies of `package.json` and `package-lock.json` only, so no code from the branch runs there.

Closing the remaining paths needs isolation at the operating-system level — a separate Unix user, a container or a virtual machine for the stages — which Bureau does not provide. Where the branch may come from someone who could not already read the operator's files, run the stages in such an isolated account.

## What to expect

Bureau is a solo-maintained project. Response times are best-effort. If a vulnerability is confirmed and material, the fix will be prioritized above whatever feature work is in flight.

For questions about this policy, use the email above.
