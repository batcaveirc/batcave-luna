#!/bin/sh
# Run every suite in this directory.
#
# CLAUDE.md's "before claiming done" step is a loop you type by hand. A loop
# you retype is a loop that eventually gets shortened, and then a suite that
# was green yesterday breaks without anybody noticing. This is the same loop
# with the failures counted, so the exit status means what it says.
#
#   sh run_tests.sh              everything
#   sh run_tests.sh memory       only suites whose name contains "memory"
#
# A suite passes by exiting 0 and fails by exiting non-zero, which is the
# convention every test_*.py here already follows.
cd "$(dirname "$0")" || exit 1
PY=${PYTHON:-python3}
filter=${1:-}

# Nine of these suites open with
#   TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
# on Python 3.9 — not a failing test but `str | None` in an annotation being
# evaluated at def time. The workflow pins 3.12, so name the requirement here
# rather than letting an interpreter mismatch look like a bug in the bridge.
"$PY" - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 10):
    sys.exit("  Python 3.10+ required (annotations use 'X | None'; the "
             "workflow runs 3.12), got " + sys.version.split()[0])
EOF

passed=0
failed=""
out=$(mktemp) || exit 1
for t in test_*.py; do
    case $t in
        *"$filter"*) ;;
        *) continue ;;
    esac
    printf '  %-24s' "$t"
    if "$PY" "$t" > "$out" 2>&1; then
        passed=$((passed + 1))
        echo 'ok'
    else
        echo 'FAIL'
        failed="$failed $t"
        grep -iE 'FAIL|Traceback|Error' "$out" | head -6 | sed 's/^/      /'
    fi
done
rm -f "$out"

if [ -n "$failed" ]; then
    echo ""
    echo "  $passed passed,$(echo $failed | wc -w | tr -d ' ') failed:$failed"
    exit 1
fi
echo "  ALL PASS ($passed suites)"
