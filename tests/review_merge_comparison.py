"""Local fake-provider comparison, not a live-model latency/cost benchmark."""
import json
import statistics
import time

from review_merge_test import PipelineTests

rows = []
for mode in ('deterministic', 'model_fallback'):
    samples = []
    for _ in range(3):
        case = PipelineTests(); case.setUp()
        try:
            if mode == 'model_fallback': case.values['performance'] = 'Malformed fixture output'
            start = time.monotonic()
            record, _, capture = case.run_review(0 if mode == 'model_fallback' else 20)
            elapsed = time.monotonic() - start
            samples.append(dict(wall_seconds=round(elapsed, 3), calls=record['calls'],
                                input_tokens=record['usage']['input_tokens'], output_tokens=record['usage']['output_tokens'],
                                merger_prompt_bytes=(capture / 'verdict.txt').stat().st_size if (capture / 'verdict.txt').exists() else 0))
        finally: case.doCleanups()
    rows.append(dict(mode=mode, samples=samples, median_wall_seconds=round(statistics.median(s['wall_seconds'] for s in samples), 3)))
print(json.dumps(rows, indent=2))
