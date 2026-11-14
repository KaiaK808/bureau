section: Fixed

- `tests/test_untrusted_env_bureau.sh` no longer depends on how the host's bash was built: it removes `SSH_CLIENT`, `SSH2_CLIENT` and `SSH_CONNECTION` before its probes, because Debian and Ubuntu bash read the bashrc files instead of `BASH_ENV` when one of them is set, which made check D's negative control fail in suites started over ssh. Repository test only. **Upgrade:** none; adopting repositories are not affected. Validation: the file passes on macOS, and its check D control passes in a suite started over ssh on an Ubuntu host where it failed before.
