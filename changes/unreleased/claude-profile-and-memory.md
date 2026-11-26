section: Security

- Disable auto-memory in every background Claude child and warn once about stages inheriting the operator profile, with a names-only user MCP server and enabled-plugin inventory and explicit warnings when profile files cannot be checked. Doctor remains read-only; drift persistence is deferred.

  **Upgrade:** Compare installed script drift, resync the complete scripts scope and restart drivers. Set a lean `agents.providers.claude.config_dir` (or `BUREAU_CLAUDE_CONFIG_DIR`) to silence the inheritance warning; log in to that profile separately. Configuration schema and interactive Claude use are unchanged.

  **Validation:** Ran fixture-only Python unit checks including warning/memory negative controls, Python compilation, Bash syntax, whitespace and changelog checks, and regenerated the site pages. The scheduled shell drivers and full suite were not run; live provider behavior, real operator profiles and adopting installations were not exercised.
