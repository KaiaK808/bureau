"""Merge successful timing TSVs into the matching OS weights, rounding up."""
import argparse
import csv
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
import re


def update_weights(weights_path, timing_paths):
    measurements = {}
    for path in timing_paths:
        with path.open(newline='') as source:
            rows = csv.DictReader(source, delimiter='\t')
            if rows.fieldnames != ['test_file', 'elapsed_seconds', 'outcome', 'os']:
                raise ValueError(f'{path}: invalid timing header')
            count = 0
            for row in rows:
                count += 1
                if (None in row or None in row.values() or row['outcome'] != 'PASS'
                        or row['os'] not in ('Linux', 'macOS')
                        or re.fullmatch(r'test_[A-Za-z0-9_-]+\.sh', row['test_file']) is None):
                    raise ValueError(f'{path}: expected successful test timings with Linux/macOS OS')
                try:
                    elapsed = Decimal(row['elapsed_seconds'])
                except InvalidOperation as error:
                    raise ValueError(f'{path}: invalid elapsed seconds') from error
                if not elapsed.is_finite() or elapsed < 0:
                    raise ValueError(f'{path}: elapsed seconds must be finite and nonnegative')
                key = row['test_file'], row['os']
                measurements[key] = max(measurements.get(key, Decimal(0)), elapsed)
            if not count:
                raise ValueError(f'{path}: no test timings')

    # Keep the slowest successful observation when several runs overlap.
    seconds = {key: str(int(value.to_integral_value(rounding=ROUND_CEILING)))
               for key, value in measurements.items()}
    with weights_path.open(newline='') as source:
        lines = source.readlines()
    known = set()
    for index, line in enumerate(lines):
        if not line.strip() or line.startswith('#'):
            continue
        fields = list(re.finditer(r'\S+', line))
        if len(fields) != 3:
            raise ValueError(f'{weights_path}: invalid weight row')
        name = fields[0][0]
        known.add(name)
        for column, os_name in ((2, 'macOS'), (1, 'Linux')):
            if (name, os_name) in seconds:
                field = fields[column]
                line = line[:field.start()] + seconds[name, os_name] + line[field.end():]
        lines[index] = line
    for name in sorted({name for name, _ in seconds} - known):
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(f"{name}\t{seconds.get((name, 'Linux'), '-')}\t{seconds.get((name, 'macOS'), '-')}\n")
    with weights_path.open('w', newline='') as target:
        target.writelines(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--weights', type=Path, default=Path(__file__).with_name('shard-weights.tsv'))
    parser.add_argument('timings', nargs='+', type=Path)
    args = parser.parse_args()
    try:
        update_weights(args.weights, args.timings)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
