# Structured review fixture comparison

Run `PYTHONDONTWRITEBYTECODE=1 python3 tests/review_merge_comparison.py` for three fresh workers per mode using the existing supervision fixture, real local Git and fake Codex/Linear/GitHub. Fixture setup is excluded; worker startup, specialist calls, build check, evidence and publication are included. The fallback forces one malformed specialist to exercise the existing merger path over the same empty findings. No network or live provider is used.

The code merge makes three provider calls rather than four. The fake CLI reports one input and one output token per call, so the fixture reports 3/3 rather than 4/4 tokens (25% fewer calls and synthetic tokens). These fixed counters are contract checks, not tokenizer or billing measurements. The code path also omits the complete merger prompt; the comparison script records its byte size. Live model token savings and wall time remain unmeasured.

Measured locally on 2026-10-29, before the full-suite shard run:

| Path | Calls | Fake input/output tokens | Merger prompt bytes | Wall seconds (3 samples) | Median wall seconds |
|---|---:|---:|---:|---|---:|
| Code merge | 3 | 3 / 3 | 0 | 11.920, 11.481, 11.966 | 11.920 |
| Model fallback | 4 | 4 / 4 | 4,578 | 12.306, 12.464, 12.700 | 12.464 |

The measured median difference was 0.544 s (4.4% of the fallback worker time); most fixture time is shared shell/Git/evidence work. The removed prompt is 4,578 UTF-8 bytes, approximately 1,145 tokens at the rough bytes/4 convention; that estimate is not an observed provider count. Timing is observational and never a test threshold. Enabling the shadow verifier adds up to eight fresh calls, so it can cost more than the previous four-call path. Malformed specialist output retains the existing four-call cost.
