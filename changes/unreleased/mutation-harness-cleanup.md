section: Fixed

- Mutation-harness cleanup retries Ctrl-C interruptions before propagating cancellation, records completion after fixture shutdown and both pipe closes, and attempts stderr closure even when stdout closure fails. Tests only; update the source checkout, with no asset resync or runtime compatibility change. Validation: focused fault-injection regressions and pre-fix negative controls; live descendant tests are blocked by sandbox process inspection, and Linux was not run.
