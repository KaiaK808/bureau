section: Fixed

- Test files and direct cancellation fixtures reset inherited ignored signals, keeping background and nohup suite results consistent without changing the runner's Ctrl-C behavior. **Upgrade:** pull the updated source for contributor tests; adopting repositories need no resync. Validation: runner probes with negative controls (with the reset removed, a test file sees the inherited ignored signals), the cancellation test run directly with SIGINT ignored on entry and through a runner started as a background job, and the touched test files.
