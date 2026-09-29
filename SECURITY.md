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

Bureau runs code a branch controls before any human has reviewed it: the review build check (`repo.test_command`, `scripts/bureau-test.sh` or `npm run build`), the three QA test runs, `repo.post_implement_command`, the Codex completion test, the app `test` action, the build and test commands of `upstream-port.sh`, and the agent processes, which run the branch's tests themselves (Claude with `--dangerously-skip-permissions`) and load the branch's own agent settings and hooks. Since v3.1 each of them starts in a reduced environment (`bureau_untrusted_env` in `scripts/bureau-env.sh`, `untrusted_env` in `scripts/bureau-provider.py`). By default it lacks `LINEAR_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID`, `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN`, and any other variable whose value contains one of their values, such as the stage's `API_KEY` or a remote URL with a token in it; with `repo.untrusted_env: "clean"` it holds only a short list of base variables (see the [configuration reference](docs/configuration.md)). The stage itself keeps the keys for its own Linear, GitHub and Telegram calls.

This closes the accidental paths: a dependency or build step that reads, logs, uploads or bundles its environment, and a test that prints its environment into a log that ends up in Linear or on a pull request. Code that deliberately looks for the keys while running as the same Unix user as Bureau can still find them. What stays open, stated so nobody relies on more:

- **Files.** The main checkout's `.env` (the file the stages read, `BUREAU_ENV_FILE`) and `.bureau.json` are readable by the same user. Stage worktrees hold no `.env` after their reset (`git clean -fdx`), and the stages skip a `repo.worktree_links` entry that is a `.env*` file (any path component starting with `.env` in any case, or an entry whose target in the main checkout is such a file) with one warning line; `bureau-doctor.py` reports such an entry as an error.
- **Stored logins.** Removing `GH_TOKEN` and `GITHUB_TOKEN` does not remove `gh`'s own login: `gh auth token` prints the token stored in `~/.config/gh/hosts.yml` or the system keychain, and a git credential helper (`git credential fill`, the macOS keychain helper) hands out the GitHub credential the same way. The same holds for any tool that keeps its login under `~/.config` or in the keychain.
- **Environments of other processes.** The stage processes hold the keys: the queue and the shepherd export `.env` to every stage, and `bureau-runtime.py` and `bureau-provider.py` run with the stage's environment. On Linux the same user can read `/proc/<pid>/environ` of any of them. On macOS there is no `/proc`, but `ps -E` reads another process's environment too: checked on macOS 26, a child process started with an empty environment read its parent's `LINEAR_API_KEY` through `ps -E -p <parent>` when the parent was Homebrew's `python3` (the interpreter that runs `bureau-runtime.py` and `bureau-provider.py`); for `/bin/bash` and `/bin/sleep` it showed none. So this path is open on both systems.
- **Everything else in the default environment.** The default removes only the Bureau-owned secrets and the GitHub token variables. An SSH agent (`SSH_AUTH_SOCK`), cloud credentials and registry tokens from the operator's environment stay; `repo.untrusted_env: "clean"` drops them.
- **The agent's own login.** `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY`, when the agent logs in through them, reach what the agent runs: the agent cannot run without its login.
- **Git hooks whose commands come from the branch.** Bureau's own `git checkout`, `git commit`, `git merge` and `git push` in a stage worktree run the repository's hooks in the stage shell, with the full stage environment. When the hook commands come from tracked files — `core.hooksPath` pointing at a tracked directory, the pre-commit framework's `.pre-commit-config.yaml`, `lefthook.yml`, husky v4's `package.json` — the branch decides what they run. Bureau does not change hook behaviour; on a Bureau host, do not install such hooks in the checkout the stages use, or point `core.hooksPath` at an untracked directory.

Not a path: the dependency restore after a worktree reset runs `npm ci --ignore-scripts` on copies of `package.json` and `package-lock.json` only, so no code from the branch runs there.

Closing the remaining paths needs isolation at the operating-system level — a separate Unix user, a container or a virtual machine for the stages — which Bureau does not provide. Where the branch may come from someone who could not already read the operator's files, run the stages in such an isolated account.

## What to expect

Bureau is a solo-maintained project. Response times are best-effort. If a vulnerability is confirmed and material, the fix will be prioritized above whatever feature work is in flight.

For questions about this policy, use the email above.
