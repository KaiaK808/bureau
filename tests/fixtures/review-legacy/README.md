# Disabled review contract fixtures

The canonical review schema is the exact v3.5.0 schema at commit `1c73640` (SHA-256 `bd3aba769b87a3ca59c36c176e669c6685ef0081f9e36a3065e0746fcb3fa220`). `codex-schema.json` fixes its recursive required/nullable wire projection, including serialization bytes.

The four provider prompts and PR comment fix the disabled output for `PipelineTests` with its default fake result. Their prompt templates and comment projection were compared with the released source. Only the two pinned commit IDs are replaced with `<BASE>` and `<HEAD>`; the remaining bytes are compared exactly. These fixtures are data, not a second runtime. Tests read no release tag or historical Git object and never regenerate expected files.
