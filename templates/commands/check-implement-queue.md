---
description: Pick one eligible Linear issue from the configured implement queue and run the corresponding Bureau workflow in the current task.
---

Read `.bureau.json` and `scripts/bureau-stage.md`. Use the configured team, project filter, label names and state UUIDs. Pick at most one issue using `pipeline_pick_next` from `scripts/bureau-config.sh`; this shares dependency and exclusion rules with background workers. Authenticated Linear tools may be used for inspection, but do not replace configured UUIDs with guessed names such as Review or Experiments.

If no eligible issue exists, report queue empty and stop. Otherwise invoke `/linear-implement` via the Skill tool with the issue identifier. Its prepare step claims issue/workspace ownership atomically before work. If another owner holds it, report the conflict and stop this pick. Finish through the shared stage protocol. Spec stops at Spec Review; implementation stops at QA/Build Review or retains blocked/partial work. Do not start a polling loop, create another app task, or merge unless the user's request separately authorizes that action.
