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

## What to expect

Bureau is a solo-maintained project. Response times are best-effort. If a vulnerability is confirmed and material, the fix will be prioritized above whatever feature work is in flight.

For questions about this policy, use the email above.
