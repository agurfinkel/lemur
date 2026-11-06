"""lemur sweep: Run Z3 on a benchmark across seeds and configurations."""

import csv
import itertools
import json
import os
import sys
from pathlib import Path

from lemur import tally as tally_mod
from lemur.cli import agent_help
from lemur.sweep import RunConfig, parse_seed_range, run_sweep
from lemur.table import make_console, output


def _parse_grid(spec: str) -> tuple[str, list[str]]:
    """Parse '--grid key=v1,v2,v3' into (key, [v1, v2, v3])."""
    if "=" not in spec:
        raise ValueError(f"--grid spec must be 'key=v1,v2,...': {spec!r}")
    key, vals = spec.split("=", 1)
    values = [v.strip() for v in vals.split(",") if v.strip()]
    if not values:
        raise ValueError(f"--grid spec has no values: {spec!r}")
    return key.strip(), values


_CONFIG_PROBE_SMT2 = "(set-logic QF_LIA) (assert true) (check-sat)\n"


def _validate_configs(z3_bin: str, configs: list[RunConfig]) -> list[tuple[str, str]]:
    """Run each config's z3 params against a trivial SMT2 input and report
    any that produce errors. Returns a list of (config_name, error_text)
    for failed configs; empty list if all pass.

    Cheap: one z3 invocation per config. The probe input is solver-trivial
    (one assert) so total wall is dominated by tactic-chain construction
    if anything. A bad chain like `tactic.default_tactic=(then` errors at
    parameter-parse time before any solving happens."""
    import subprocess

    bad: list[tuple[str, str]] = []
    for cfg in configs:
        if not cfg.params:
            continue
        cmd = [z3_bin] + [f"{k}={v}" for k, v in cfg.params.items()]
        cmd.append("-in")
        try:
            proc = subprocess.run(
                cmd,
                input=_CONFIG_PROBE_SMT2,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            bad.append((cfg.name, f"z3 probe timed out ({z3_bin})"))
            continue
        except FileNotFoundError:
            bad.append((cfg.name, f"z3 binary not found: {z3_bin}"))
            continue
        # Concatenate stdout + stderr; either may carry the error message.
        combined = (proc.stdout or "") + (proc.stderr or "")
        if "(error" in combined:
            err_lines = [
                line.strip()
                for line in combined.splitlines()
                if line.strip().startswith("(error")
            ]
            err_text = "; ".join(err_lines) or combined.strip()
            bad.append((cfg.name, err_text))
    return bad


def _parse_split(spec: str) -> tuple[str, str]:
    """Parse '--split name:smt-to-inject' into (name, smt)."""
    if ":" not in spec:
        raise ValueError(f"--split spec must be 'name:smt': {spec!r}")
    name, smt = spec.split(":", 1)
    name = name.strip()
    smt = smt.strip()
    if not name or not smt:
        raise ValueError(f"--split spec has empty name or smt: {spec!r}")
    return name, smt


def register(subparsers):
    p = subparsers.add_parser(
        "sweep",
        help="Run Z3 across seeds and configurations",
        epilog="AI agents: use `lemur sweep --agent` for terse usage guide.",
    )
    agent_help.add_agent_flag(p, "sweep")
    p.add_argument("benchmark", help="SMT2 benchmark file")
    p.add_argument(
        "--seeds",
        default="0-3",
        help="Seed range: 0-15, 1,3,5, or 0-3,7 (default: 0-3)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Timeout per run in seconds (default: 30)",
    )
    p.add_argument(
        "--config",
        action="append",
        default=[],
        help='Config spec: "name: key=val key=val". Repeatable. '
        'Quote values with whitespace: key="(then simplify smt)".',
    )
    p.add_argument(
        "--grid",
        action="append",
        default=[],
        help='Grid spec: "key=v1,v2,v3". Repeatable. '
        "Cross-products into configs, combined with --config as bases.",
    )
    p.add_argument(
        "--split",
        action="append",
        default=[],
        help='Split spec: "name:<smt-to-inject>". Repeatable. '
        "Injected before (check-sat). Cross-products with configs × seeds.",
    )
    p.add_argument(
        "--z3",
        default=None,
        help="Path to z3 binary (default: ~/ag/z3/z3-edge/build/z3)",
    )
    p.add_argument(
        "--jobs",
        "-j",
        default="1",
        help="Parallel jobs: int or 'auto' (= os.cpu_count()). Default: 1.",
    )
    p.add_argument(
        "--trace",
        default=None,
        help="Comma-separated trace tags to enable (e.g., nla_solver,nra)",
    )
    p.add_argument(
        "--verbosity",
        type=int,
        default=2,
        help="Z3 verbosity level, -v:N (default: 2, 0 to disable)",
    )
    p.add_argument(
        "--z3-log",
        action="store_true",
        help="Enable z3 AST trace log (trace=true). Requires --save.",
    )
    p.add_argument(
        "--save",
        default=None,
        help="Directory to save per-run artifacts. File names: "
        "<config>_s<seed>.{stdout,stderr,trace,z3log,stats.json} "
        "without splits, or <split>.<config>_s<seed>.{ext} "
        "with --split / directory mode.",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Enable z3 -st statistics; with --save writes a "
        ".stats.json file per run (same naming as --save).",
    )
    p.add_argument(
        "--format",
        "-f",
        choices=["rich", "plain", "json"],
        default=None,
        help="Output format (default: rich for TTY, plain otherwise)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable color output")
    p.add_argument(
        "--no-commands", action="store_true", help="Hide z3 command lines from output"
    )
    p.add_argument(
        "--tally",
        action="store_true",
        help="Print per-config aggregation after results",
    )
    p.add_argument(
        "--stop-on",
        choices=["sat", "unsat"],
        default=None,
        help="Abort the whole sweep on first run matching this status",
    )
    p.add_argument(
        "--stop-on-per-split",
        choices=["sat", "unsat"],
        default=None,
        help="Scope --stop-on to each split: close a split on its first "
        "matching run, then skip remaining runs for that split only. "
        "Requires --split. Incompatible with --stop-on.",
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Abort sweep on first timeout/unknown/error "
        "(composes with --stop-on-per-split). Implies "
        "--stop-on-error.",
    )
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Abort sweep on first run with status=error "
        "(scoped narrower than --fail-fast, which also "
        "covers timeout/unknown).",
    )
    p.add_argument(
        "--no-config-check",
        action="store_true",
        help="Skip the pre-flight z3 dry-run that catches malformed "
        "configs (a single z3 invocation per config on a "
        "trivial SMT2 input). Auto-skipped under -f plain "
        "on the assumption that plain output means scripted "
        "/ agent use, where config typos are unlikely.",
    )
    p.set_defaults(func=run)


def run(args):
    # Resolve benchmark path. May be a single .smt2 file OR a directory
    # produced by `lemur split` (with a plan.json manifest).
    benchmark = Path(args.benchmark).resolve()
    if not benchmark.exists():
        print(f"Error: benchmark file not found: {benchmark}", file=sys.stderr)
        sys.exit(1)

    leaf_files = None
    pre_closed_splits = None
    if benchmark.is_dir():
        # Directory mode: plan.json is the authoritative manifest.
        if args.split:
            print(
                "Error: directory mode (sweep DIR/) is incompatible with --split; "
                "the directory's plan.json is already the split manifest.",
                file=sys.stderr,
            )
            sys.exit(2)
        from lemur.split import SplitError, read_plan

        try:
            plan = read_plan(str(benchmark))
        except SplitError as e:
            print(f"Error: {e}", file=sys.stderr)
            print(
                "       (sweep DIR/ expects a plan.json produced by `lemur split`.)",
                file=sys.stderr,
            )
            sys.exit(2)
        leaf_files = []
        pre_closed_splits = {}
        for leaf in plan.leaves:
            if leaf.pruned:
                # Synthesize a split name from the valuation tuple so it
                # shows up in the CSV/tally. Mirror the emitter's naming.
                label = "_".join("T" if v else "F" for v in leaf.valuation.values())
                pre_closed_splits[f"leaf_{label}"] = leaf.reason or "pruned"
            elif leaf.file:
                name = Path(leaf.file).stem
                path = benchmark / leaf.file
                if not path.exists():
                    print(
                        f"Warning: plan.json lists {leaf.file} but the file "
                        f"is missing; skipping",
                        file=sys.stderr,
                    )
                    continue
                leaf_files.append((name, str(path)))
        if not leaf_files and not pre_closed_splits:
            print(
                f"Error: {benchmark}/plan.json has no leaves to sweep.", file=sys.stderr
            )
            sys.exit(2)
        # `benchmark` is a directory; run_sweep's `smt_file` parameter is
        # used only as a fallback when neither splits nor leaf_files is set,
        # so passing a placeholder string is harmless, but we stringify the
        # dir path just for downstream display consistency.
        benchmark_str_for_sweep = str(benchmark)
    else:
        benchmark_str_for_sweep = str(benchmark)

    # Resolve z3 binary
    z3_bin = args.z3 or str(Path.home() / "ag/z3/z3-edge/build/z3")
    z3_path = Path(z3_bin)
    if not z3_path.exists():
        print(f"Error: z3 binary not found: {z3_bin}", file=sys.stderr)
        sys.exit(1)
    z3_bin = str(z3_path.resolve())

    # Resolve --jobs (int or 'auto')
    if isinstance(args.jobs, str) and args.jobs.lower() == "auto":
        jobs = os.cpu_count() or 1
    else:
        try:
            jobs = int(args.jobs)
        except (TypeError, ValueError):
            print(
                f"Error: --jobs must be an integer or 'auto' (got {args.jobs!r})",
                file=sys.stderr,
            )
            sys.exit(1)

    # Parse seeds
    seeds = parse_seed_range(args.seeds)

    # Parse base configs (from --config)
    configs = []
    if args.config:
        for spec in args.config:
            configs.append(RunConfig.parse(spec))

    # Apply --grid expansion: cross-product of grid values, multiplied by
    # each base config (or applied standalone if no --config given).
    if args.grid:
        try:
            grid_specs = [_parse_grid(g) for g in args.grid]
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        grid_keys = [k for k, _ in grid_specs]
        value_lists = [vs for _, vs in grid_specs]

        bases = configs if configs else [RunConfig(name="", params={})]
        configs = []
        for base in bases:
            for combo in itertools.product(*value_lists):
                params = dict(base.params)
                for k, v in zip(grid_keys, combo):
                    params[k] = v
                combo_name = "_".join(combo)
                name = f"{base.name}.{combo_name}" if base.name else combo_name
                configs.append(RunConfig(name=name, params=params))

    if not configs:
        configs.append(RunConfig(name="default", params={}))

    # Parse splits
    splits = None
    if args.split:
        try:
            splits = [_parse_split(s) for s in args.split]
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # Validate --stop-on-per-split early, before any stdout header is written.
    # Requires either --split or directory-mode leaves; incompatible with
    # --stop-on; composes with --fail-fast.
    if args.stop_on_per_split:
        if not splits and leaf_files is None:
            print(
                "Error: --stop-on-per-split requires either --split or a "
                "directory (sweep DIR/) as the benchmark positional.",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.stop_on:
            print(
                "Error: --stop-on-per-split cannot be combined with --stop-on "
                "(use --fail-fast for global infrastructure aborts).",
                file=sys.stderr,
            )
            sys.exit(2)

    # Parse trace tags
    trace_tags = None
    if args.trace:
        trace_tags = [t.strip() for t in args.trace.split(",")]

    # Validate --z3-log requires --save
    if args.z3_log and not args.save:
        print(
            "Error: --z3-log requires --save (log files need a destination)",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine output format
    fmt = args.format
    effective_fmt = (
        fmt if fmt is not None else ("rich" if sys.stdout.isatty() else "plain")
    )
    show_progress = effective_fmt == "rich" and sys.stdout.isatty()

    console = make_console(no_color=args.no_color) if effective_fmt == "rich" else None

    # Pre-flight config validation. Catches typos like
    #   --config 'D: tactic.default_tactic=(then ...)'
    # where the unquoted parens silently truncate the param value. Cheap
    # (one z3 call per config on a trivial SMT2 input). Skip under plain
    # output on the assumption that plain = scripted/agent use; humans
    # benefit most from the early-fail.
    if not args.no_config_check and effective_fmt != "plain":
        bad = _validate_configs(z3_bin, configs)
        if bad:
            for cfg_name, msg in bad:
                print(
                    f"Error: config {cfg_name!r} failed pre-flight check:",
                    file=sys.stderr,
                )
                print(f"  {msg}", file=sys.stderr)
            print(
                "Re-run with --no-config-check to bypass this check.", file=sys.stderr
            )
            sys.exit(2)

    # Any mode that carries a `split` dimension in results: explicit --split,
    # or directory-mode leaf_files / pre_closed_splits.
    has_splits = bool(splits) or bool(leaf_files) or bool(pre_closed_splits)

    if show_progress and console:
        console.print(f"[bold]lemur sweep[/bold] {benchmark.name}")
        console.print(f"  z3: {z3_bin}")
        console.print(f"  seeds: {seeds[0]}-{seeds[-1]} ({len(seeds)} seeds)")
        console.print(f"  configs: {', '.join(c.name for c in configs)}")
        console.print(f"  timeout: {args.timeout}s, jobs: {jobs}")
        if leaf_files is not None:
            n_live = len(leaf_files)
            n_pre = len(pre_closed_splits) if pre_closed_splits else 0
            console.print(
                f"  leaves: {n_live} live + {n_pre} pre-closed "
                f"(from {benchmark}/plan.json)"
            )
        if trace_tags:
            console.print(f"  trace: {', '.join(trace_tags)}")
        console.print()

    # Stream CSV rows to stdout as they complete (plain format only).
    on_result = None
    if effective_fmt == "plain":
        writer = csv.writer(sys.stdout)
        header = (
            ["split", "config", "seed", "status", "time_s"]
            if has_splits
            else ["config", "seed", "status", "time_s"]
        )
        writer.writerow(header)
        sys.stdout.flush()

        if has_splits:

            def on_result(r):
                writer.writerow(
                    [r.split or "", r.config, r.seed, r.status, f"{r.time_s:.3f}"]
                )
                sys.stdout.flush()
        else:

            def on_result(r):
                writer.writerow([r.config, r.seed, r.status, f"{r.time_s:.3f}"])
                sys.stdout.flush()

    # Build the early-termination predicates.
    stop_when = None
    if args.stop_on or args.fail_fast or args.stop_on_error:
        if args.fail_fast:
            fail_statuses = {"timeout", "unknown", "error"}
        elif args.stop_on_error:
            fail_statuses = {"error"}
        else:
            fail_statuses = set()
        target = args.stop_on  # 'sat' | 'unsat' | None

        def stop_when(r):
            return r.status == target or r.status in fail_statuses

    stop_per_split_when = None
    if args.stop_on_per_split:
        per_target = args.stop_on_per_split

        def stop_per_split_when(r):
            return r.status == per_target

    table, results = run_sweep(
        z3_bin=z3_bin,
        smt_file=benchmark_str_for_sweep,
        seeds=seeds,
        configs=configs,
        timeout=args.timeout,
        jobs=jobs,
        trace_tags=trace_tags,
        verbosity=args.verbosity,
        z3_log=args.z3_log,
        save_dir=args.save,
        show_progress=show_progress,
        on_result=on_result,
        stop_when=stop_when,
        stats=args.stats,
        splits=splits,
        stop_per_split_when=stop_per_split_when,
        leaf_files=leaf_files,
        pre_closed_splits=pre_closed_splits,
    )

    if show_progress and console:
        console.print()

    # Plain rows were already streamed; only render final table for rich/json.
    # Skip the flat SweepTable when splits are used (it doesn't model the split
    # dimension); the tally below gives the per-(split, config) view instead.
    if effective_fmt != "plain" and not has_splits:
        output(table, fmt=effective_fmt, console=console)

    # Per-config aggregation. Force-on when splits are in play, since the
    # flat SweepTable above was skipped.
    if (args.tally or has_splits) and results:
        tally = tally_mod.compute_tally(results)
        if effective_fmt == "rich":
            if console:
                console.print()
            tally_mod.render_rich(
                tally, console or make_console(no_color=args.no_color)
            )
        elif effective_fmt == "json":
            print(tally_mod.to_json(tally))
        else:  # plain
            print()
            print(tally_mod.to_csv(tally), end="")

    # Print command lines for manual re-run
    if results and not args.no_commands:
        seen_configs = set()
        cmds = []
        for r in results:
            if r.config not in seen_configs and r.cmdline:
                seen_configs.add(r.config)
                cmds.append((r.config, r.cmdline))

        if fmt == "json":
            cmd_data = {config: cmdline for config, cmdline in cmds}
            print(json.dumps({"commands": cmd_data}, indent=2))
        elif fmt == "plain":
            print()
            for config, cmdline in cmds:
                print(f"# {config}")
                print(cmdline)
        else:
            if console:
                console.print()
                console.print(
                    "[bold]Commands[/bold] [dim](change seeds to re-run)[/dim]"
                )
                # Print each command on its own line, unboxed, so it's
                # copy-paste friendly. The `# name` comment line keeps a
                # subtle style but the command itself is plain text.
                for config, cmdline in cmds:
                    console.print(f"[bold dim]# {config}[/bold dim]")
                    console.print(
                        cmdline, highlight=False, markup=False, soft_wrap=True
                    )
