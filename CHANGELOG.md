# Changelog

Bureau tracks changes via merged PR titles on `main`. This file summarizes the notable ones in reverse chronological order. Every entry links to its PR so the details are one click away.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning isn't enforced — Bureau doesn't cut tags; `main` is always the shipping branch. Adopters upgrade via `git pull` in `~/.claude/skills/bureau-init/` or `/bureau-init --resync-scripts` in a target repo.

## Unreleased

### Open-source launch preparation

- **CI runner moved off self-hosted** — GitHub-hosted `ubuntu-latest` is now the default (was `[self-hosted, linux, ARM64]` on a production Hetzner box). Removes the fork-PR-RCE class for public-repo PR CI. Least-privilege `GITHUB_TOKEN` scope (`contents: read`) added top-level. ([#28](https://github.com/KaiaK808/bureau/pull/28))
- **Proprietary Atipo Foundry fonts removed** — the six Babcock + Silka `.woff2` files that shipped in `docs/site/assets/fonts/` had a non-redistributable EULA; the MIT license couldn't cover them. Working tree cleaned; CSS falls through to a system-sans stack. Downstream sites with a valid Atipo license can layer their own `@font-face` blocks to restore the display faces transparently. ([#29](https://github.com/KaiaK808/bureau/pull/29))
- **Quick fixes** — clone remote updated to `KaiaK808/bureau`, `.gitignore` gets `.env` / `.pem` / `.log` rules, internal infra references genericized in committed public files, template default flipped to `ubuntu-latest`. ([#30](https://github.com/KaiaK808/bureau/pull/30))
- **Supply-chain hygiene** — `actions/checkout` pinned to full SHA, `.github/dependabot.yml` added for the `github-actions` ecosystem so pinned actions get bumped automatically. ([#31](https://github.com/KaiaK808/bureau/pull/31))
- **Community + discoverability** — `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1), `.env.example`, `.github/ISSUE_TEMPLATE/config.yml`, `NOTICE` documenting third-party asset provenance.
- **Open-source hygiene** — `CONTRIBUTING.md`, `SECURITY.md`, issue + PR templates, README badges, MIT LICENSE. ([#23](https://github.com/KaiaK808/bureau/pull/23), [#27](https://github.com/KaiaK808/bureau/pull/27))
- **Rebrand** — project renamed from "bureau-init" to "Bureau" in prose; repo renamed on GitHub; slash command `/bureau-init` unchanged. On-brand hero image added (`docs/assets/hero.png` — a dim ceremonial archive chamber, wall of drawers, one glowing hot pink). ([#24](https://github.com/KaiaK808/bureau/pull/24), [#25](https://github.com/KaiaK808/bureau/pull/25))

### Token efficiency

- **`/goal` + caveman + Headroom** — three opt-in compression layers in `.bureau.json` `agents.*`. `/goal` replaces the implement-pipeline retry loop with Claude Code's native slash command (Haiku evaluates per turn); caveman compresses review-prose output ~65%; Headroom wraps the `claude` binary for 60–95% input-token reduction on tool-output-heavy stages. Composes for ~80% fewer tokens per pipeline tick. ([#22](https://github.com/KaiaK808/bureau/pull/22), [#26](https://github.com/KaiaK808/bureau/pull/26))

### Pipeline & upstream-port fixes

- Codex code-review no longer captures stderr (fixed `ARG_MAX` overflow on merge), severity floor for security findings, caveman wiring in review prose, headroom `wrap claude --` separator ([#26](https://github.com/KaiaK808/bureau/pull/26))
- Upstream-port `--with-llm` flag — Claude-assisted conflict resolution with cost gate ([#13](https://github.com/KaiaK808/bureau/pull/13))
- Upstream-port `.bureau-port-map.json` for path renames + dropped files ([#12](https://github.com/KaiaK808/bureau/pull/12))
- Upstream-port script itself, mirrored from brainhuggers-cli ([#11](https://github.com/KaiaK808/bureau/pull/11))
- Implement-pipeline: match tasks.md by leading number ([#2](https://github.com/KaiaK808/bureau/pull/2)), ready-flip on PARTIAL+commits ([#8](https://github.com/KaiaK808/bureau/pull/8)), stuck-detector skip on COMPLETE ([#9](https://github.com/KaiaK808/bureau/pull/9)), PARTIAL+prior-commits doesn't force STUCK ([#10](https://github.com/KaiaK808/bureau/pull/10))

### Infrastructure

- Self-hosted CI runner migration to `openclaw-01` (later reverted for public-repo safety, see #28) ([#6](https://github.com/KaiaK808/bureau/pull/6))
- Model-resolution precedence fix — env > per-stage JSON > workspace JSON > env default, live-read ([#4](https://github.com/KaiaK808/bureau/pull/4))
- Baseline test-suite fixes ([#7](https://github.com/KaiaK808/bureau/pull/7))

For anything not summarized here, `git log --oneline main` is authoritative.
