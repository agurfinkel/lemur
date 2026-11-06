"""
Trace file parser for Z3 TRACE/CTRACE output.

Trace format:
  -------- [TAG] function_name /path/to/file.cpp:LINE ---------
  <free-form body, possibly multi-line>
  ------------------------------------------------

STRACE entries have no header/footer — they are not parsed by this module.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO

# Matches: -------- [TAG] function_name /path/file.cpp:LINE ---------
HEADER_RE = re.compile(r"^-{8} \[([^\]]+)\] (\S+) (.+?):(\d+) -{9}$")
FOOTER_RE = re.compile(r"^-{48}$")


@dataclass
class TraceEntry:
    """A single trace block parsed from a .z3-trace file."""

    tag: str
    function: str
    source_file: str
    source_line: int
    body: str
    line_number: int  # line in the trace file where this entry starts

    def body_lines(self) -> list[str]:
        return self.body.splitlines()


def parse_trace(source: TextIO | str | Path) -> Iterator[TraceEntry]:
    """Parse a Z3 trace file, yielding TraceEntry for each block.

    Accepts a file object, file path string, or Path.
    Skips lines that don't belong to a recognized header/footer structure.
    """
    if isinstance(source, (str, Path)):
        with open(source, "r", errors="replace") as f:
            yield from _parse_stream(f)
    else:
        yield from _parse_stream(source)


def _parse_stream(f: TextIO) -> Iterator[TraceEntry]:
    tag = function = source_file = None
    source_line = 0
    body_lines: list[str] = []
    header_line_num = 0
    in_block = False

    for line_num, raw_line in enumerate(f, 1):
        line = raw_line.rstrip("\n")

        if not in_block:
            m = HEADER_RE.match(line)
            if m:
                tag = m.group(1)
                function = m.group(2)
                source_file = m.group(3)
                source_line = int(m.group(4))
                header_line_num = line_num
                body_lines = []
                in_block = True
        else:
            if FOOTER_RE.match(line):
                yield TraceEntry(
                    tag=tag,
                    function=function,
                    source_file=source_file,
                    source_line=source_line,
                    body="\n".join(body_lines),
                    line_number=header_line_num,
                )
                in_block = False
            else:
                body_lines.append(line)


def filter_by_tag(entries: Iterator[TraceEntry], *tags: str) -> Iterator[TraceEntry]:
    """Filter trace entries to only those matching given tags."""
    tag_set = set(tags)
    for entry in entries:
        if entry.tag in tag_set:
            yield entry


def filter_by_function(
    entries: Iterator[TraceEntry], *functions: str
) -> Iterator[TraceEntry]:
    """Filter trace entries to only those matching given function names."""
    func_set = set(functions)
    for entry in entries:
        if entry.function in func_set:
            yield entry


def group_by_tag(entries: list[TraceEntry]) -> dict[str, list[TraceEntry]]:
    """Group a list of trace entries by tag."""
    groups: dict[str, list[TraceEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.tag, []).append(entry)
    return groups


def group_by_function(entries: list[TraceEntry]) -> dict[str, list[TraceEntry]]:
    """Group a list of trace entries by function name."""
    groups: dict[str, list[TraceEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.function, []).append(entry)
    return groups


# --- Varmap parsing ---

# Matches entries like: j25=103: R21  or  _t95=257: (+ -1 R76 R83)
_VARMAP_ENTRY_RE = re.compile(r"([A-Za-z_]\w*)=\d+:\s+(.*?)(?=\s+[A-Za-z_]\w+=\d+:|$)")


def parse_varmap_line(line: str) -> dict[str, str]:
    """Parse a single varmap: line into a {jvar: smt_expr} dict.

    Line format: varmap: j25=103: R21 j28=113: (div R20 R21) ...
    Returns empty dict if the line isn't a varmap line.
    """
    prefix = "varmap:"
    if not line.startswith(prefix):
        return {}
    payload = line[len(prefix) :]
    result: dict[str, str] = {}
    for m in _VARMAP_ENTRY_RE.finditer(payload):
        var_name = m.group(1)
        smt_expr = m.group(2).strip()
        if smt_expr:
            result[var_name] = smt_expr
    return result


def collect_varmap(entries: list[TraceEntry]) -> dict[str, str]:
    """Build a global j-variable to SMT-expression map from all varmap lines.

    Scans all trace entries for varmap: lines in the body and unions them.
    Returns empty dict if no varmap lines are found (graceful degradation).
    """
    varmap: dict[str, str] = {}
    for entry in entries:
        for line in entry.body_lines():
            stripped = line.strip()
            if stripped.startswith("varmap:"):
                varmap.update(parse_varmap_line(stripped))
    return varmap
