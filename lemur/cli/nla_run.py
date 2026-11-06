"""lemur nla-run: capture an nla_solver trace + analyze it in one shot.

Replaces the manual three-step ritual:

    mkdir /tmp/trace && cd /tmp/trace
    build/z3 -tr:nla_solver -T:30 -st tactic.default_tactic="..." bench.smt2
    lemur nla /tmp/trace/.z3-trace

with a single call. The z3 invocation reuses `lemur.sweep.run_single` so
process-group isolation, tmpdir cleanup, and trace capture come for free.
Remaining args (after `--`) flow through to `lemur nla`, so any analysis
flag (`--list`, `--detail N`, `--strategy SUB`, etc.) just works.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from lemur.cli import agent_help

_DEFAULT_Z3 = str(Path.home() / "ag/z3/z3-edge/build/z3")


def register(subparsers):
    p = subparsers.add_parser(
        "nla-run",
        help="Capture an nla_solver trace and analyze it in one call.",
        epilog="Pass nla flags after `--`. Example: "
        "lemur nla-run BENCH.smt2 --seed 0 -- --list. "
        "AI agents: use `lemur nla-run --agent`.",
    )
    agent_help.add_agent_flag(p, "nla-run")
    p.add_argument("benchmark", help="SMT2 benchmark file")
    p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="z3 random seed (sat/smt/nlsat). Default: 0.",
    )
    p.add_argument(
        "--timeout", type=int, default=30, help="z3 timeout in seconds. Default: 30."
    )
    p.add_argument(
        "--tactic",
        metavar="TACTIC",
        default=None,
        help="Value for tactic.default_tactic. Quote SMT2 chains "
        "(e.g. '(then simplify smt)').",
    )
    p.add_argument(
        "--config",
        metavar="K=V",
        action="append",
        default=[],
        help="Additional z3 param (repeatable; e.g. --config smt.arith.solver=2).",
    )
    p.add_argument(
        "--z3", metavar="PATH", default=None, help=f"z3 binary. Default: {_DEFAULT_Z3}"
    )
    p.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete the trace tmpdir on exit; print its "
        "path. Useful when you want to re-run lemur nla "
        "with different flags.",
    )
    p.add_argument(
        "nla_args", nargs="*", default=[], help=argparse.SUPPRESS
    )  # documented via epilog
    p.set_defaults(func=run)


def _parse_kv(items: list[str], target: dict) -> None:
    for spec in items:
        if "=" not in spec:
            raise SystemExit(f"Error: --config spec must be K=V, got {spec!r}")
        k, _, v = spec.partition("=")
        target[k.strip()] = v.strip()


def run(args):
    nla_extra = list(args.nla_args)

    bench = Path(args.benchmark).resolve()
    if not bench.exists():
        print(f"Error: benchmark not found: {bench}", file=sys.stderr)
        sys.exit(1)

    # `--x-form` (varmap source) works from `-tr:nla_solver` alone; only
    # the older `--x-form-source nra` path needs the expensive [nra] tag.
    trace_tags = ["nla_solver"]
    if "--x-form-source" in nla_extra:
        idx = nla_extra.index("--x-form-source")
        if idx + 1 < len(nla_extra) and nla_extra[idx + 1] == "nra":
            trace_tags.append("nra")

    # Defer the heavy import until we know we're going to run.
    from lemur import sweep as sweep_mod

    params: dict = {}
    if args.tactic:
        params["tactic.default_tactic"] = args.tactic
    try:
        _parse_kv(args.config, params)
    except SystemExit:
        raise

    cfg = sweep_mod.RunConfig(name="default", params=params)
    z3_bin = args.z3 or _DEFAULT_Z3

    # `delete=not args.keep` would be cleaner but requires Python 3.12.
    tmpdir = tempfile.mkdtemp(prefix="lemur_nla_run_")
    cleanup = not args.keep
    try:
        # save_dir gives us a stable trace path; run_single still uses its
        # own scratch dir under tmpdir for the actual z3 invocation.
        result = sweep_mod.run_single(
            z3_bin=z3_bin,
            smt_file=str(bench),
            seed=args.seed,
            config=cfg,
            timeout=args.timeout,
            trace_tags=trace_tags,
            save_dir=tmpdir,
            stats=False,
        )

        trace_path = Path(tmpdir) / f"default_s{args.seed}.trace"
        if not trace_path.exists() or trace_path.stat().st_size == 0:
            print(
                f"Error: no nla_solver trace was produced "
                f"(z3 status: {result.status}, time: {result.time_s:.2f}s).",
                file=sys.stderr,
            )
            if result.stderr:
                print(f"--- z3 stderr ---\n{result.stderr.rstrip()}", file=sys.stderr)
            sys.exit(1)

        # Note z3's exit status non-fatally — useful info, not a failure.
        print(f"# z3 status: {result.status} ({result.time_s:.2f}s)", file=sys.stderr)
        print(f"# trace: {trace_path}", file=sys.stderr)

        nla_argv = ["lemur", "nla", str(trace_path)] + nla_extra
        proc = subprocess.run(nla_argv, check=False)
        sys.exit(proc.returncode)
    finally:
        if cleanup:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"# kept tmpdir: {tmpdir}", file=sys.stderr)
