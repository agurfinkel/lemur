"""
Parsers for `z3 -st` output.

z3 with `-st` prints a result line (sat / unsat / unknown / timeout) followed by a
trailing S-expression of statistics:

    unsat
    (:added-eqs                  100
     :max-memory                  31.42
     :memory                       5.79
     ...)

Lives in its own module so callers that only need to parse don't drag in
sweep.py's process-pool / progress-bar machinery.
"""

import re

_STATS_BLOCK_RE = re.compile(r"\(\s*(:[^()]*)\)\s*\Z")
_STATS_KV_RE = re.compile(r":([\w\-.]+)\s+(\S+)")
_RESULT_RE = re.compile(r"^(sat|unsat|unknown|timeout)\s*$", re.MULTILINE)


def parse_z3_stats(stdout: str) -> dict | None:
    """Extract z3 `-st` statistics block from stdout.

    Returns a dict keyed by stat name with values parsed to int / float /
    string. Returns None if no trailing stats block is found.
    """
    m = _STATS_BLOCK_RE.search(stdout)
    if not m:
        return None
    stats: dict = {}
    for match in _STATS_KV_RE.finditer(m.group(1)):
        key, val = match.group(1), match.group(2)
        try:
            stats[key] = int(val)
        except ValueError:
            try:
                stats[key] = float(val)
            except ValueError:
                stats[key] = val
    return stats or None


def parse_z3_run(stdout: str) -> tuple[str | None, dict | None]:
    """Parse a single z3 `-st` invocation's stdout.

    Returns `(result, stats)`:
      - result: 'sat', 'unsat', 'unknown', 'timeout', or None when no recognizable
        line precedes the stats block. For multi-(check-sat) outputs the
        last result token before the stats block is returned.
      - stats: dict from `parse_z3_stats`, or None.
    """
    stats = parse_z3_stats(stdout)
    if stats is not None:
        m = _STATS_BLOCK_RE.search(stdout)
        head = stdout[: m.start()] if m else stdout
    else:
        head = stdout
    results = _RESULT_RE.findall(head)
    return (results[-1] if results else None, stats)
