"""Export Z3 text traces as lossless JSONL with optional derived annotations."""

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from lemur.cli import agent_help
from lemur.trace_recognizers import BVRecognizer, recognize
from lemur.trace_stream import trace_events


def register(subparsers):
    parser = subparsers.add_parser("export", help="Export .z3-trace as lossless JSONL")
    agent_help.add_agent_flag(parser, "export")
    parser.add_argument("trace", help="Trace file, or - for stdin")
    parser.add_argument(
        "-o", "--output", default="-", help="JSONL destination (default: stdout)"
    )
    parser.add_argument("--format", choices=["jsonl"], default="jsonl")
    parser.add_argument(
        "--recognizer",
        choices=["bv"],
        action="append",
        default=[],
        help="Add domain annotations; default is raw framing only",
    )
    parser.set_defaults(func=run)


def run(args):
    try:
        if args.trace != "-" and args.output != "-":
            source, target = Path(args.trace), Path(args.output)
            if source.resolve() == target.resolve() or (
                target.exists() and os.path.samefile(source, target)
            ):
                raise ValueError("input and output must be different files")
        with ExitStack() as stack:
            source = (
                sys.stdin.buffer
                if args.trace == "-"
                else stack.enter_context(open(args.trace, "rb"))
            )
            target = (
                sys.stdout
                if args.output == "-"
                else stack.enter_context(
                    open(args.output, "w", encoding="utf-8", newline="\n")
                )
            )
            recognizers = [BVRecognizer()] if "bv" in args.recognizer else []
            for record in recognize(trace_events(source), recognizers):
                target.write(
                    json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
                )
            target.flush()
    except BrokenPipeError:
        # Avoid another write during interpreter shutdown after a consumer exits.
        sys.stdout = open(os.devnull, "w")
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
