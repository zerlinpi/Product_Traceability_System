"""Fail the build if the test suite shrinks.

The easiest way to make a red build green is to delete the failing test. This
turns that into a second, louder failure: the collected count is compared against
a committed baseline, and any drop is an error.

Raising the baseline is expected and normal — when you add tests, update it:

    python tools/check_test_count.py --update

Lowering it is the thing that needs justifying, so the tool prints what it is
doing and why it refuses.

Run without arguments to check (exit 0 = ok, 1 = the suite shrank).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "BASELINE_COUNT"

# pytest's summary line, e.g. "819 tests collected in 12.34s" or
# "1 error" / "no tests ran".
COUNT_RE = re.compile(r"(\d+)\s+tests?\s+collected")


def collect_count() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    matches = COUNT_RE.findall(output)
    if not matches:
        print("could not determine the collected test count from pytest output:")
        print(output.strip()[-2000:])
        raise SystemExit(2)
    return int(matches[-1])


def read_baseline() -> int:
    if not BASELINE.exists():
        print(f"missing {BASELINE}; create it with: python tools/check_test_count.py --update")
        raise SystemExit(2)
    text = BASELINE.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return int(stripped)
    raise SystemExit(2)


def main() -> int:
    count = collect_count()

    if "--update" in sys.argv:
        BASELINE.write_text(
            f"# Minimum number of tests CI requires.\n"
            f"# Raise it when you add tests (python tools/check_test_count.py --update).\n"
            f"# Lowering it needs a reason in the commit message: deleting tests is not\n"
            f"# how a failing build gets fixed.\n"
            f"{count}\n",
            encoding="utf-8",
        )
        print(f"baseline updated to {count}")
        return 0

    baseline = read_baseline()
    print(f"collected {count} tests; baseline {baseline}")
    if count < baseline:
        print(
            f"\nFAIL: the suite shrank by {baseline - count} test(s).\n"
            "If tests were deliberately removed, lower the baseline in the same\n"
            "commit and say why in the message.",
            file=sys.stderr,
        )
        return 1
    if count > baseline:
        print(
            f"note: {count - baseline} more test(s) than the baseline — "
            "run with --update to raise it."
        )
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
