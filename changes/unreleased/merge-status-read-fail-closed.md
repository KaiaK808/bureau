section: Fixed

- A failed legacy commit-status read now holds the CI merge gate as not yet decided and names the head SHA, even when check runs are green; existing decided CI blockers keep precedence. **Upgrade:** resync the scripts scope; a merge that could pass before with an unreadable status list now waits. The explicit CI opt-out is unchanged. Validation: focused CI helper and merge-stage tests, including fail-before controls, plus changelog and rendered-doc checks; provider calls are faked and the full suite is left to the maintainer.
