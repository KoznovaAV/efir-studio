"""Produce the CI reports and compare exact executable line counts to the gate."""

import json
from pathlib import Path
import subprocess
import sys


def coverage(*args, capture=False):
    return subprocess.run(
        [sys.executable, "-m", "coverage", *map(str, args)],
        check=True, text=True, encoding="utf-8", capture_output=capture,
    ).stdout


def main():
    directory = Path("reports/qa")
    directory.mkdir(parents=True, exist_ok=True)
    coverage("combine")
    coverage("html", "-d", directory / "html")
    coverage("xml", "-o", directory / "coverage.xml")
    target = directory / "coverage.json"
    coverage("json", "-o", target)
    totals = json.loads(target.read_text(encoding="utf-8"))["totals"]
    statements = totals["num_statements"]
    lines = totals["covered_lines"]
    branches = totals["num_branches"]
    summary = (f"Line coverage: {100 * lines / statements:.2f}% ({lines}/{statements})\n"
               f"Branch coverage: {100 * totals['covered_branches'] / branches:.2f}% "
               f"({totals['covered_branches']}/{branches})\n")
    report = coverage("report", capture=True) + "\n" + summary
    if lines * 100 < 95 * statements:
        report += "FAIL: executable line coverage is below 95%.\n"
    (directory / "coverage.txt").write_text(report, encoding="utf-8")
    print(report, end="")
    return int(lines * 100 < 95 * statements)


if __name__ == "__main__":
    sys.exit(main())
