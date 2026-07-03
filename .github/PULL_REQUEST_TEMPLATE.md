<!--
Thanks for the PR. Follow the shape of the existing merged PRs on `main` —
short Summary, What changed, Test plan, After merge. See any of the recent
merged PRs (e.g. #22, #24, #26) for the reference shape.

Small PRs merge fast. Wide PRs get pushed back to smaller ones.
-->

## Summary

<!-- 1-3 sentences on why this change is here. What problem does it solve? -->

## What changed

<!-- Bullet list of the concrete changes. Reference file paths. -->

-
-

## Test plan

- [ ] `bash tests/run.sh` — all green
- [ ] `bash -n` clean on any modified shell scripts
- [ ] `shellcheck -S warning -e SC1091,SC2034,SC2155,SC2086` silent on modified pipeline scripts
- [ ] Manual smoke test relevant to the change (describe)

## Docs

<!-- Docs travel with code. If this changes behaviour, which docs did you update? -->

## After merge

<!-- What still needs to happen after this lands (target-repo resync, follow-up ticket, etc.). Delete if nothing. -->
