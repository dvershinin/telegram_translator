"""Tests ensuring all pipeline stages use the same date logic.

Regression: publisher.py used UTC while digest.py used local time,
causing publish failures when the cron ran between midnight UTC and
midnight local time (e.g., 04:00 WITA = 20:00 UTC previous day).
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "telegram_translator"


def _find_default_date_expressions(filepath: Path) -> list[tuple[int, str]]:
    """Find lines where a date defaults via datetime.now().

    Returns:
        List of (line_number, source_line) tuples for date default expressions.
    """
    hits = []
    source = filepath.read_text()
    for i, line in enumerate(source.splitlines(), 1):
        # Match patterns like: date = date or datetime.now(...
        # and: datetime.now(...).strftime("%Y-%m-%d")
        if re.search(r"datetime\.now\(.*\)\.strftime\(.%Y-%m-%d.\)", line):
            hits.append((i, line.strip()))
    return hits


def test_no_utc_in_date_defaults():
    """All date-default expressions must use local time, not UTC.

    The pipeline stages (digest.py, publisher.py, etc.) must agree on
    what 'today' means. Using timezone.utc in one place and local time
    in another causes date mismatches during overnight cron runs.
    """
    violations = []
    for py_file in SRC.glob("*.py"):
        for lineno, line in _find_default_date_expressions(py_file):
            if "utc" in line.lower():
                violations.append(f"{py_file.name}:{lineno}: {line}")

    assert not violations, (
        "Date defaults must use local time (datetime.now()), not UTC. "
        "Mixing timezones causes publish failures during overnight cron.\n"
        "Violations:\n" + "\n".join(violations)
    )


def test_digest_and_publisher_date_logic_match():
    """DigestPipeline._today() and PodcastPublisher.publish() must use the same date logic."""
    digest_src = (SRC / "digest.py").read_text()
    publisher_src = (SRC / "publisher.py").read_text()

    # Extract the _today() body from digest.py
    match = re.search(
        r"def _today\(self\).*?return (datetime\.now\(\)\.strftime\(.+?\))",
        digest_src,
        re.DOTALL,
    )
    assert match, "Could not find _today() in digest.py"
    digest_expr = match.group(1)

    # The publisher's date default must use the same expression
    assert digest_expr in publisher_src, (
        f"publisher.py must use the same date expression as digest.py's _today(): "
        f"{digest_expr}"
    )
