---
name: Bug report
about: Something in the pipeline isn't behaving as documented
title: ""
labels: bug
---

## What happened

<!-- One-paragraph description of what went wrong. What did you run, what did you expect, what did you get. -->

## Steps to reproduce

<!-- The exact command sequence. Ideally against a fresh /bureau-init install so nothing is stateful. -->

1. …
2. …
3. …

## Expected behaviour

<!-- What you thought should happen. Cite the doc if you're going off a specific docs claim. -->

## Actual behaviour

<!-- What actually happened. Paste the relevant terminal output or logs. -->

<details>
<summary>Terminal output / logs</summary>

```
(paste here)
```

</details>

## Environment

- Bureau source tag and commit (run in the actual source clone; say "unknown" for a legacy copy): `git describe --tags --always --dirty` and `git rev-parse HEAD`
- Operating mode: Codex app task / Claude Code interactive / bounded background / continuous background
- Selected background provider and model, if used:
- Relevant app/CLI versions (`codex --version` and/or `claude --version` only when those CLIs are used):
- OS + version:
- Source installation path and requested install target (`claude` / `codex` / `both`):
- Relevant doctor output, with credentials and private project details removed:
- Target repo language/stack (if relevant):
- Which pipeline stage: (`implement` / `qa` / `code_review` / `merge` / etc.)

## Anything else

<!-- Related tickets, workarounds you've tried, hypothesis about the cause, etc. Optional. -->
