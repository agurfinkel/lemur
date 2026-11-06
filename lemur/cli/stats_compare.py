"""lemur stats-compare: Compare z3 -st stats across configs.

Two input modes, auto-detected:
  1. One positional that is a directory containing
     `<config>_s<seed>.stats.json` files (sweep --save layout).
  2. One or more positional files holding raw `z3 -st` stdout
     (a result line followed by the trailing stats S-expression).
     Each file's basename-stem is its config label, unless overridden
     with `--label NAME=GLOB` (repeatable).
"""

import glob as glob_mod
import sys
from pathlib import Path

from lemur.cli import agent_help
from lemur.stats_compare import (
    load_stats_dir,
    load_stats_files,
    render_rich,
    to_csv,
    to_json,
)
from lemur.table import make_console


def register(subparsers):
    p = subparsers.add_parser(
        "stats-compare",
        help="Compare z3 -st stats across configs (sweep --save dir, or raw z3 -st files)",
        epilog="AI agents: use `lemur stats-compare --agent` for terse usage guide.",
    )
    agent_help.add_agent_flag(p, "stats-compare")
    p.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        default=[],
        help="Either one directory with <config>_s<seed>.stats.json "
        "files (sweep --save layout), or zero or more raw z3 "
        "-st output files. In raw mode each file's "
        "basename-stem is its label; if you only use --label, "
        "no positional is required.",
    )
    p.add_argument(
        "--label",
        action="append",
        default=None,
        metavar="NAME=GLOB",
        help="Raw mode only: assign multiple files to one label. "
        "GLOB is a literal path or shell-style glob; repeat for "
        "multiple labels. Files passed positionally are still "
        "included (with their stem as label).",
    )
    p.add_argument(
        "--top",
        type=int,
        default=None,
        help="Limit output to the N stats with the largest mean magnitude",
    )
    p.add_argument(
        "--format",
        "-f",
        choices=["rich", "plain", "json"],
        default=None,
        help="Output format (default: rich for TTY, plain otherwise)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable color output")
    p.set_defaults(func=run)


def _build_specs(
    positionals: list[str], label_specs: list[str]
) -> list[tuple[str, str]]:
    """Build (label, path) pairs from CLI args.

    Positional files use their basename-stem as label. `--label NAME=GLOB`
    contributes one or more files (matched via glob) under NAME.
    """
    specs: list[tuple[str, str]] = []
    for p in positionals:
        if Path(p).is_dir():
            print(
                f"Error: cannot mix files and directories in raw mode "
                f"(got directory: {p})",
                file=sys.stderr,
            )
            sys.exit(2)
        specs.append((Path(p).stem, p))
    for ls in label_specs or []:
        if "=" not in ls:
            print(f"Error: --label expects NAME=GLOB (got {ls!r})", file=sys.stderr)
            sys.exit(2)
        name, pattern = ls.split("=", 1)
        if not name:
            print(
                f"Error: --label NAME must not be empty (got {ls!r})", file=sys.stderr
            )
            sys.exit(2)
        matches = sorted(glob_mod.glob(pattern))
        if not matches:
            print(f"Error: --label {name}={pattern}: no files matched", file=sys.stderr)
            sys.exit(2)
        for m in matches:
            specs.append((name, m))
    return specs


def run(args):
    paths = args.paths
    label_specs = args.label or []

    if not paths and not label_specs:
        print("Error: pass at least one PATH or --label NAME=GLOB", file=sys.stderr)
        sys.exit(2)

    is_dir_mode = len(paths) == 1 and Path(paths[0]).is_dir() and not label_specs

    if is_dir_mode:
        directory = Path(paths[0]).resolve()
        if not directory.exists():
            print(f"Error: directory not found: {directory}", file=sys.stderr)
            sys.exit(1)
        try:
            cmp = load_stats_dir(str(directory))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if not cmp.configs:
            print(f"Error: no *.stats.json files found in {directory}", file=sys.stderr)
            sys.exit(1)
    else:
        specs = _build_specs(paths, label_specs)
        if not specs:
            print("Error: no input files specified", file=sys.stderr)
            sys.exit(1)
        cmp = load_stats_files(specs)
        if not cmp.configs:
            print(
                "Error: no parseable z3 -st content in any input file", file=sys.stderr
            )
            sys.exit(1)

    fmt = args.format
    effective_fmt = (
        fmt if fmt is not None else ("rich" if sys.stdout.isatty() else "plain")
    )

    if effective_fmt == "rich":
        render_rich(cmp, make_console(no_color=args.no_color), top=args.top)
    elif effective_fmt == "plain":
        print(to_csv(cmp, top=args.top), end="")
    elif effective_fmt == "json":
        print(to_json(cmp, top=args.top))
