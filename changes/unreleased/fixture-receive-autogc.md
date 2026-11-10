section: Fixed

- Test fixture repositories also disable `receive.autogc`, so a push to a fixture origin on older Git (2.43, the stock Git of Ubuntu 24.04) no longer starts `git gc --auto`, which ignores `maintenance.auto`. **Upgrade:** nothing to do; this concerns only the repository's own tests. Validation: in an `ubuntu:24.04` container with Git 2.43.0, `test_shared_runtime.sh`, `test_supervision_pipeline.sh` and `test_fixture_hygiene.sh` pass; the same files from main fail both maintenance trace tests with the `git gc --auto --quiet` child. On macOS Git 2.50.1 the touched tests pass.
