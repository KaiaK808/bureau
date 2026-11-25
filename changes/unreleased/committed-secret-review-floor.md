section: Added

- Always scan added committed branch lines for known credential formats and long, high-entropy secret assignments. Masked findings and not-measured scans prevent review approval; base-controlled scanner exceptions require an owner, reason and future expiry. This local check needs no scanner binary or database and does not inspect intermediate history or decoded content.

  **Upgrade:** Compare installed runtime drift, resync the entire scripts scope and restart drivers together. Review synthetic fixtures for narrow base exceptions; no Trivy setup or enable switch is required.

  Validation: Pure-Python rule/hunk unit checks, Python compilation, Bash syntax, whitespace and changelog checks, and static site rendering were run. Real Git fixture tests, pipeline/mutation drivers, the full suite and real Trivy qualification were not run; the maintainer must run the registered secret tests and required suites.
