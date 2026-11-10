# Changelog fragments

For this repository, add one `changes/unreleased/<slug>.md` per change; slugs
contain only lowercase letters, digits and hyphens. Do not add entries directly
to `CHANGELOG.md` under Unreleased. Use UTF-8 without trailing whitespace:

```markdown
section: Fixed

- What changed. **Upgrade:** adopter actions and compatibility limits. Validation: checks and remaining limits.
```

The section is exactly `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
`Security` or `Documentation`. Include one non-empty `- ` list item; indent any
continuation lines by at least two spaces, with no additional list items.
Keep `unreleased/.gitkeep` so the directory survives an empty release.

Run `python3 scripts/changelog.py check` to validate and `python3 scripts/changelog.py render`
to preview without writes. Entries use the section order above and filename order
within each section. Maintainers run `python3 scripts/changelog.py release VERSION DATE`
(for example `3.8.0 2026-11-10`) to insert a dated section atomically and delete the
used fragments. Empty releases, duplicate versions and invalid dates are refused.
Fragments stay on disk in `changes/.release-*` staging directories until the
changelog is replaced. Failures before replacement restore them by rename;
failed restores or cleanup leave the staging path and remaining files on stderr.
`check` refuses while any staging directory remains. Inspect `CHANGELOG.md`:
if the dated section is absent, move the staged files back to `unreleased/`;
if present, verify its entries before deleting the staged files. Remove the empty
staging directory and run `check` before retrying.
Exit codes follow `scripts/release_verify.py`: 0 success, 1 validation failure,
2 usage or I/O error. This workflow does not affect adopting repositories.
