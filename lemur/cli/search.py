"""lemur search: Regex search over .z3-trace entries, with tag/function filters."""

import re
import sys
from pathlib import Path

from rich.text import Text

from lemur.cli import agent_help
from lemur.parsers import parse_trace
from lemur.table import make_console


def register(subparsers):
    p = subparsers.add_parser(
        "search",
        help="Regex search over .z3-trace body lines, filtered by tag/function",
        epilog="AI agents: use `lemur search --agent` for terse usage guide.",
    )
    agent_help.add_agent_flag(p, "search")
    p.add_argument("trace", help="Path to .z3-trace file")
    p.add_argument(
        "pattern",
        nargs="?",
        default=None,
        help="Regex to match against body lines (omit to match every "
        "line in entries passing --tag/--fn filters)",
    )
    p.add_argument(
        "--tag",
        default=None,
        metavar="REGEX",
        help="Filter to entries whose tag matches this regex "
        '(re.search; use ^/$ to anchor, e.g., "^nla")',
    )
    p.add_argument(
        "--fn",
        default=None,
        metavar="REGEX",
        help="Filter to entries whose function matches this regex",
    )
    p.add_argument(
        "--entries",
        action="store_true",
        help="Print whole matching entries (header + body) instead of lines",
    )
    p.add_argument(
        "-n",
        "--line-number",
        action="store_true",
        help="Prefix matched lines with their line number in the trace file",
    )
    p.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
        help="Case-insensitive pattern match",
    )
    p.add_argument(
        "-v",
        "--invert",
        action="store_true",
        help="Show lines/entries that do NOT match",
    )
    p.add_argument(
        "-c",
        "--count",
        action="store_true",
        help="Print only the total count of matches",
    )
    p.add_argument(
        "--max-count", type=int, default=None, metavar="N", help="Stop after N matches"
    )
    p.add_argument(
        "--format",
        "-f",
        choices=["rich", "plain"],
        default=None,
        help="Output format (default: rich for TTY, plain otherwise)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable color output")
    p.set_defaults(func=run)


def _compile(pattern: str | None, ignore_case: bool, label: str) -> re.Pattern:
    # None means "match everything".
    pat = pattern if pattern is not None else ""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pat, flags)
    except re.error as e:
        print(f"Error: invalid {label} regex {pat!r}: {e}", file=sys.stderr)
        sys.exit(2)


def run(args):
    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"Error: trace file not found: {trace_path}", file=sys.stderr)
        sys.exit(1)

    pattern_re = _compile(args.pattern, args.ignore_case, "pattern")
    tag_re = _compile(args.tag, args.ignore_case, "tag") if args.tag else None
    fn_re = _compile(args.fn, args.ignore_case, "fn") if args.fn else None

    fmt = args.format
    effective_fmt = (
        fmt if fmt is not None else ("rich" if sys.stdout.isatty() else "plain")
    )
    console = make_console(no_color=args.no_color) if effective_fmt == "rich" else None

    match_count = 0
    entry_match_count = 0

    def line_matches(line: str) -> bool:
        m = bool(pattern_re.search(line))
        return (not m) if args.invert else m

    def emit_line(entry, body_idx: int, line: str):
        abs_line = entry.line_number + 1 + body_idx
        if console and effective_fmt == "rich":
            prefix = Text()
            if args.line_number:
                prefix.append(f"{abs_line}:", style="dim")
            rich_line = Text(line)
            # Highlight actual matches (not applicable when inverted).
            if not args.invert and args.pattern:
                for m in pattern_re.finditer(line):
                    rich_line.stylize("bold red", m.start(), m.end())
            console.print(prefix + rich_line)
        else:
            if args.line_number:
                sys.stdout.write(f"{abs_line}:")
            sys.stdout.write(line)
            sys.stdout.write("\n")

    def emit_entry(entry, matching_line_indices: list[int]):
        abs_header = entry.line_number
        if console and effective_fmt == "rich":
            header = Text()
            if args.line_number:
                header.append(f"{abs_header}:", style="dim")
            header.append(
                f"-------- [{entry.tag}] {entry.function} "
                f"{entry.source_file}:{entry.source_line} --------",
                style="bold cyan",
            )
            console.print(header)
        else:
            if args.line_number:
                sys.stdout.write(f"{abs_header}:")
            sys.stdout.write(
                f"-------- [{entry.tag}] {entry.function} "
                f"{entry.source_file}:{entry.source_line} --------\n"
            )

        body = entry.body_lines()
        matching = set(matching_line_indices)
        for i, line in enumerate(body):
            abs_line = entry.line_number + 1 + i
            is_match = i in matching
            if console and effective_fmt == "rich":
                prefix = Text()
                if args.line_number:
                    prefix.append(f"{abs_line}:", style="dim")
                rich_line = Text(line)
                if is_match and not args.invert and args.pattern:
                    for m in pattern_re.finditer(line):
                        rich_line.stylize("bold red", m.start(), m.end())
                console.print(prefix + rich_line)
            else:
                if args.line_number:
                    sys.stdout.write(f"{abs_line}:")
                sys.stdout.write(line)
                sys.stdout.write("\n")

        if console and effective_fmt == "rich":
            console.print(Text("-" * 48, style="dim"))
        else:
            sys.stdout.write("-" * 48 + "\n")

    try:
        for entry in parse_trace(trace_path):
            if tag_re is not None and not tag_re.search(entry.tag):
                continue
            if fn_re is not None and not fn_re.search(entry.function):
                continue

            body = entry.body_lines()
            matched_indices = [i for i, line in enumerate(body) if line_matches(line)]
            if not matched_indices:
                continue

            if args.count:
                match_count += len(matched_indices)
                entry_match_count += 1
                if args.max_count is not None and (
                    (args.entries and entry_match_count >= args.max_count)
                    or (not args.entries and match_count >= args.max_count)
                ):
                    break
                continue

            if args.entries:
                emit_entry(entry, matched_indices)
                entry_match_count += 1
                match_count += len(matched_indices)
                if args.max_count is not None and entry_match_count >= args.max_count:
                    break
            else:
                for idx in matched_indices:
                    emit_line(entry, idx, body[idx])
                    match_count += 1
                    if args.max_count is not None and match_count >= args.max_count:
                        raise _StopSearch()
    except _StopSearch:
        pass
    except BrokenPipeError:
        # Piped to head/less etc.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return

    if args.count:
        if args.entries:
            print(f"{entry_match_count} entries, {match_count} lines")
        else:
            print(match_count)

    # Exit 1 when nothing matched — matches grep convention.
    if match_count == 0:
        sys.exit(1)


class _StopSearch(Exception):
    pass
