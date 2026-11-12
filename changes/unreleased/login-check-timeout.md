section: Fixed

- Provider login checks retry a timeout once, with limits of 20 s then 60 s; two timeouts return the existing retryable exit 16 with `login-check-timeout`, and doctor warns that login is unconfirmed after a timeout. **Upgrade:** update Bureau, compare local runtime changes and resync the scripts scope. Validation: provider login tests in normal and isolated-copy modes with delayed fake Claude/Codex responses and child-process exit assertions; exact doctor timeout and missing-login warning assertions; negative controls for response waiting and the isolated doctor copy.
