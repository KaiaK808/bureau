# Spec Kit 1.2.2 feature-helper fixture

`speckit-1.2.2.zip` holds unmodified upstream source files from the pinned tag, plus its MIT license. Runtime installation remains owned by `bureau_install.py`; this archive is test-only and extracted in throwaway repositories. Providers/network are faked in tests.

| File | Source | SHA-256 |
|---|---|---|
| `scripts/bash/create-new-feature.sh` | [v1.2.2 helper](https://raw.githubusercontent.com/github/spec-kit/v1.2.2/scripts/bash/create-new-feature.sh) | `972f03ed8ca5ee17b7baad6d56d821d46269d77abc337496beafb0fe85a725f2` |
| `scripts/bash/common.sh` | [v1.2.2 common](https://raw.githubusercontent.com/github/spec-kit/v1.2.2/scripts/bash/common.sh) | `53cb1c23e349db4586c272436ce35c6e2f36a9c0b5351a85e0b216d9887a26b7` |
| `LICENSE` | [v1.2.2 license](https://raw.githubusercontent.com/github/spec-kit/v1.2.2/LICENSE) | `2510b446bc1f0cf9702453075d20cd88631e20e5642658edb7325d9c1eb534f7` |

Verified contract: `--json --number N --short-name NAME --allow-existing-branch DESCRIPTION`. The positional description is required. JSON exposes `BRANCH_NAME`, `SPEC_FILE`, `FEATURE_NUM`; the helper persists `.specify/feature.json` and creates under `specs/`, without creating a Git branch. `--number` may auto-correct an occupied prefix; exact-directory reuse needs `--allow-existing-branch`. Tests assert the requested number, retry content preservation and refusal when the helper changes the number.
