section: Fixed

- Preserve quoted configuration bytes when stripping trailing Trivy ignore comments, leaving ambiguous ignore lines unchanged with reduced coverage, and validate indented Yarn lockfile bodies while retaining recognised Yarn v1 and Berry forms. **Upgrade:** resync the scripts scope; nothing else changes. Validation: offline Trivy parser and pipeline regressions, including fail-before checks, Python compilation and changelog validation; live scanner and adopter qualification are not included.
