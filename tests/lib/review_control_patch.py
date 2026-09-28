"""Put an old form back into a sandbox copy of code-review-pipeline.sh, for negative controls.

Usage: review_control_patch.py <path-to-stage> <name>

A patch that cannot find its anchor exits 3 and prints CONTROL PATCH FAILED, so a broken
control is never mistaken for a caught defect. Names:

  old          the verdict block before decide_review_verdict (text from $OLD_BLOCK)
  oldread      the cycle count that read a failed Linear read as cycle 0
  oldcrit      the CRITICAL count read from the review after the ARG_MAX trim
  oldfallback  the text verdict that took the first verdict word anywhere on the lines
"""
import os
import sys

OLD_READ = (
    'REVIEW_CYCLE_COUNT=$(get_issue_comments "$ISSUE" \\\n'
    '  | jq \'[.[] | select(.body | test("Code Review.*Changes Requested"))] | length\' 2>/dev/null || echo "0")\n'
)
EARLY_CRIT = "_sec_critical=$(parse_claude_json \"$SECURITY_REVIEW\" '.counts.critical')\n"
NEW_FALLBACK = '  VERDICT=$(review_verdict_from_text "$MERGED_REVIEW")\n'
OLD_FALLBACK = (
    '  VERDICT=$(echo "$MERGED_REVIEW" \\\n'
    "    | sed 's/\\*\\*//g' \\\n"
    "    | grep -A1 -E '^#+[[:space:]]*REVIEW_VERDICT[[:space:]]*$|^REVIEW_VERDICT:' \\\n"
    "    | grep -oE '(APPROVE|REQUEST_CHANGES|BLOCK)' \\\n"
    '    | head -1 || true)\n'
)


def main(path, name):
    src = open(path).read()

    def cut(start, end, repl, end_inclusive=True):
        i = src.index(start)
        j = src.index(end, i) + (len(end) if end_inclusive else 0)
        return src[:i] + repl + src[j:]

    if name == 'old':
        src = cut('# The verdict rules run as one ordered decision',
                  'done <<< "$(printf \'%s\\n\' "$_decision" | tail -n +2)"\n',
                  os.environ['OLD_BLOCK'] + '\n')
    elif name == 'oldread':
        src = cut('_comments_rc=0\n', 'CYCLE_NOTE=', OLD_READ, end_inclusive=False)
    elif name == 'oldcrit':
        if src.count(EARLY_CRIT) != 1:
            raise ValueError('early CRITICAL read not found')
        src = src.replace(EARLY_CRIT, '', 1)
        d = src.index('_decision=$(decide_review_verdict')
        src = src[:d] + EARLY_CRIT + src[d:]
    elif name == 'oldfallback':
        if src.count(NEW_FALLBACK) != 1:
            raise ValueError('fallback call not found')
        src = src.replace(NEW_FALLBACK, OLD_FALLBACK, 1)
    else:
        raise ValueError('unknown control ' + name)
    open(path, 'w').write(src)


if __name__ == '__main__':
    try:
        main(sys.argv[1], sys.argv[2])
    except (ValueError, KeyError) as e:
        print('CONTROL PATCH FAILED: %s: %s' % (sys.argv[2], e), file=sys.stderr)
        sys.exit(3)
