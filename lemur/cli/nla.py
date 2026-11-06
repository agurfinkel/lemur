"""lemur nla: NLA solver lemma analysis."""

import sys
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text as RichText

from lemur.cli import agent_help
from lemur.lemma import LemmaAnalyzer
from lemur.parsers import collect_varmap, group_by_tag, parse_trace
from lemur.report import (
    _SMT_EXPR_MAX_LEN,
    expand_lemma_ranges,
    humanize_varmap,
    lemma_summary_rows,
    parse_lemma_ranges,
    render_lemma_detail,
    render_lemma_detail_plain,
    render_lemma_list_plain,
    render_lemma_list_rich,
)
from lemur.round import RoundLemmaRange, lemma_ranges_per_round, parse_round_data
from lemur.table import make_console


def register(subparsers):
    p = subparsers.add_parser(
        "nla",
        help="NLA solver lemma analysis",
        epilog="AI agents: use `lemur nla --agent` for terse usage guide.",
    )
    agent_help.add_agent_flag(p, "nla")
    p.add_argument("trace", help="Path to .z3-trace file")
    p.add_argument(
        "--format",
        "-f",
        choices=["rich", "plain", "json"],
        default=None,
        help="Output format (default: rich for TTY, plain otherwise)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable color output")
    p.add_argument(
        "--no-varmap",
        action="store_true",
        help="Show raw LP j-variables instead of SMT names",
    )
    p.add_argument(
        "--no-truncate",
        action="store_true",
        help="Show full SMT names in --detail/--details and --list. "
        "Default truncates to 40 chars (one-line list mode). "
        'Use this when a detail shows "... " on lemma literals '
        "and you want the entire expression.",
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--list", "-l", action="store_true", help="List all lemmas, one per line"
    )
    mode.add_argument(
        "--detail",
        "-d",
        type=int,
        default=None,
        metavar="N",
        help="Show full variable table for Nth lemma (1-based)",
    )
    mode.add_argument(
        "--details",
        type=str,
        default=None,
        metavar="RANGE",
        help="Show detail for lemma ranges: 3, 5:10, 2-4, :5, 12:",
    )
    mode.add_argument(
        "--x-form",
        action="store_true",
        help="Stable nlsat-call fingerprints from the [nra] "
        "constraint pool. Reports total calls, unique "
        "fingerprints, top repeats, size distribution. "
        "Reads [nra] entries from the same trace, or "
        "from --nra-trace PATH if separately captured.",
    )

    p.add_argument(
        "--x-form-source",
        choices=["auto", "varmap", "nra"],
        default="auto",
        help="Which trace data backs --x-form fingerprints. "
        "`varmap` (preferred): per-lemma varmap snapshot from "
        "-tr:nla_solver alone — no extra capture cost. "
        "`nra`: x* notation from -tr:nra blocks. NOTE that "
        "-tr:nra blows up the trace ~8x (15MB → 125MB on "
        "a 30s timeout). `auto` picks varmap when "
        "~lemma_builder + varmap entries are present, else "
        "falls back to nra. Default: auto.",
    )
    p.add_argument(
        "--nra-trace",
        default=None,
        metavar="PATH",
        help="Path to a separately-captured -tr:nra trace; only "
        "consulted when --x-form-source is `nra` (or `auto` "
        "falls back). If unset and the nra path is taken, "
        "[nra] entries must be in TRACE.",
    )
    p.add_argument(
        "--top",
        type=int,
        default=10,
        metavar="N",
        help="Cap top-repeat rows in --x-form mode (default 10)",
    )
    p.add_argument(
        "--coarse",
        action="store_true",
        help='Coarse ("structural") fingerprints for --x-form: '
        "collapse integer/rational literals to LIT and "
        "alpha-rename #NNN aux IDs by first appearance, so "
        "lemmas that differ only in threshold values or aux-"
        "Bool atom selection bucket into one shape. Surfaces "
        "near-fixed-point cascades that the fine fingerprint "
        'reads as "diverse exploration". See '
        "z3-research/lemur/structural-fingerprint-proposal.md.",
    )
    p.add_argument(
        "--show",
        action="store_true",
        help="In --x-form output, print one canonicalized "
        "representative lemma signature under each top-"
        "repeats row. With --coarse the representative "
        "shows the structural shape (LIT / #A0 placeholders); "
        "without --coarse the raw varmap-resolved signature. "
        'Closes the "fingerprint-hash-only" gap when you want '
        "to know what the dominant shape actually is.",
    )
    p.add_argument(
        "--target-only",
        action="store_true",
        help="Cascade-diagnostic view: fingerprint each lemma by "
        "just its target monomial (the LP variable being "
        "bounded/split/related), with --coarse normalization "
        "applied. Drops preconditions + conclusion thresholds, "
        "keeps only the structural anchor. For each target "
        "reports a strategy crosstab (which lemma families "
        "emitted against it). Strict relaxation of --coarse; "
        "over-aggregates productive runs but surfaces tight "
        "cascades on a small monomial set in one screen. "
        "Requires --x-form. See z3-research/lemur/"
        "target-only-fingerprint-proposal.md.",
    )

    p.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of lemma previews in summary (default: 5)",
    )
    p.add_argument(
        "--delta-limit",
        type=int,
        default=5,
        help="Max variable change lines in summary (default: 5)",
    )

    # Round-productivity thresholds. Defaults are tentative on N=3 traces
    # from the kvault VC (see ../z3-research/lemur/burst-stats-proposal.md
    # for empirics); calibrate per benchmark family before treating either
    # as a verdict. Per-instance exploration tool, not a general-purpose
    # cascade detector.
    p.add_argument(
        "--productivity-threshold",
        type=float,
        default=0.35,
        metavar="F",
        help="Round-productivity rate (less-share) below which "
        "the summary marks the trace ⚠ unproductive "
        "(default: 0.35; tentative on a small corpus).",
    )
    p.add_argument(
        "--yield-threshold",
        type=float,
        default=0.40,
        metavar="F",
        help="Eviction yield (monomials evicted / lemmas emitted) "
        "below which the summary marks the trace ⚠ low yield "
        "(default: 0.40; tentative on a small corpus).",
    )

    # Filters (apply to list/detail/details/summary modes consistently; filtered
    # results are renumbered from 1).
    p.add_argument(
        "--strategy",
        action="append",
        default=[],
        metavar="SUBSTR",
        help="Keep lemmas whose strategy contains SUBSTR (case-insensitive). "
        "Repeatable: matches any.",
    )
    p.add_argument(
        "--min-vars",
        type=int,
        default=None,
        metavar="N",
        help="Keep lemmas with >= N variables",
    )
    p.add_argument(
        "--min-preconds",
        type=int,
        default=None,
        metavar="N",
        help="Keep lemmas with >= N preconditions",
    )
    p.add_argument(
        "--min-monomials",
        type=int,
        default=None,
        metavar="N",
        help="Keep lemmas with >= N monomials",
    )
    p.add_argument(
        "--top-by",
        choices=["vars", "preconds", "monomials"],
        default=None,
        metavar="FIELD",
        help="Sort descending by this field; use with --top-n",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=None,
        metavar="N",
        help="Limit to top N after --top-by sort",
    )
    p.add_argument(
        "--sample",
        metavar="STRATEGY=N",
        default=None,
        help="After other filters, pick N lemmas evenly spread "
        "across those whose strategy matches STRATEGY "
        "(case-insensitive substring). Replaces a manual "
        "`--list | awk -F. ...` pipeline. If no mode flag is "
        "set, defaults to --list output for the picked N.",
    )
    p.add_argument(
        "--sample-nlsat",
        type=int,
        default=None,
        metavar="N",
        help="Shorthand for --sample nlsat=N (the most common case).",
    )

    # Round-aware filters. Trace must carry decide+pop_scope+nla_solver
    # tags (capture via `lemur sweep --trace
    # nla_solver,decide,pop_scope,arith_conflict`); same data backs
    # `lemur n-over-time`. Mutually exclusive — --round picks one round,
    # --by-round groups all rounds.
    round_grp = p.add_mutually_exclusive_group()
    round_grp.add_argument(
        "--round",
        type=int,
        default=None,
        metavar="N",
        help="Restrict to lemmas in round N (1-based). "
        "A round runs from one ~lemma_builder "
        "emission until a pop_scope drops the SAT "
        "level below that emission's level.",
    )
    round_grp.add_argument(
        "--by-round",
        action="store_true",
        help="Group --list output by round, with a "
        "header per round noting the lemma index "
        "range and whether the round closed before "
        "the trace ended.",
    )
    p.set_defaults(func=run)


def _apply_filters(
    records, *, strategies, min_vars, min_preconds, min_monomials, top_by, top_n
):
    """Filter lemma records by strategy / size thresholds, then optionally
    sort-and-truncate by a field."""
    out = records
    if strategies:
        subs = [s.lower() for s in strategies]
        out = [
            r for r in out if r.strategy and any(s in r.strategy.lower() for s in subs)
        ]
    if min_vars is not None:
        out = [r for r in out if len(r.variables) >= min_vars]
    if min_preconds is not None:
        out = [r for r in out if len(r.preconditions) >= min_preconds]
    if min_monomials is not None:
        out = [r for r in out if len(r.monomials) >= min_monomials]
    if top_by is not None:
        key_fn = {
            "vars": lambda r: len(r.variables),
            "preconds": lambda r: len(r.preconditions),
            "monomials": lambda r: len(r.monomials),
        }[top_by]
        out = sorted(out, key=key_fn, reverse=True)
        if top_n is not None:
            out = out[:top_n]
    return out


def _parse_sample_spec(spec: str) -> tuple[str, int]:
    """Split a `STRATEGY=N` spec into its parts. Errors with a clear
    message on malformed input."""
    if "=" not in spec:
        print(f"Error: --sample expects STRATEGY=N (got {spec!r})", file=sys.stderr)
        sys.exit(2)
    strategy_substr, _, n_str = spec.partition("=")
    try:
        n = int(n_str)
    except ValueError:
        print(
            f"Error: --sample N must be a positive integer (got {n_str!r})",
            file=sys.stderr,
        )
        sys.exit(2)
    if n <= 0:
        print("Error: --sample N must be > 0", file=sys.stderr)
        sys.exit(2)
    return strategy_substr.strip().lower(), n


def _apply_sample(records, strategy_substr: str, n: int):
    """Pick N lemmas evenly spread across those whose strategy contains
    `strategy_substr` (case-insensitive). If fewer than N match, returns
    them all. Preserves original relative order in the output.

    Spread = `i * total / n` for i in [0, n). On total=10 N=4 the picks
    are at indices 0, 2, 5, 7. On total=4 N=4 all four are picked.
    """
    matches = [
        r for r in records if r.strategy and strategy_substr in r.strategy.lower()
    ]
    total = len(matches)
    if total == 0 or n >= total:
        return matches
    seen: set[int] = set()
    picked: list = []
    for i in range(n):
        idx = (i * total) // n
        if idx in seen:
            continue
        seen.add(idx)
        picked.append(matches[idx])
    return picked


def run(args):
    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"Error: trace file not found: {trace_path}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "target_only", False) and not getattr(args, "x_form", False):
        print(
            "Error: --target-only requires --x-form (target extraction "
            "uses the varmap-resolved view).",
            file=sys.stderr,
        )
        sys.exit(2)

    if getattr(args, "x_form", False):
        _run_xform(args, trace_path)
        return

    # Parse trace, filter to nla_solver tag
    entries = list(parse_trace(trace_path))
    by_tag = group_by_tag(entries)
    nla_entries = by_tag.get("nla_solver", [])

    if not nla_entries:
        print("No nla_solver entries found in trace.", file=sys.stderr)
        sys.exit(1)

    # Extract varmap and lemmas
    varmap = humanize_varmap(collect_varmap(nla_entries))
    if args.no_varmap:
        varmap = {}

    lemma_records = list(LemmaAnalyzer(nla_entries).extract())

    # Round-aware slicing & grouping. Both --round and --by-round require
    # decide/pop_scope/nla_solver in the trace. Map original lemma index
    # by entry.line_number (preserved across filters) so --by-round can
    # group post-filter records back into their source round.
    round_ranges: list[RoundLemmaRange] | None = None
    orig_idx_by_lineno: dict[int, int] = {}
    if args.round is not None or args.by_round:
        rd = parse_round_data(trace_path, warn=False)
        round_ranges = lemma_ranges_per_round(rd)
        if not round_ranges:
            print(
                "Error: no rounds detected in trace. --round / --by-round "
                "need -tr:decide -tr:pop_scope -tr:nla_solver in the "
                "capture (capture via `lemur sweep --trace "
                "nla_solver,decide,pop_scope,arith_conflict`).",
                file=sys.stderr,
            )
            sys.exit(1)
        orig_idx_by_lineno = {
            rec.entry.line_number: i + 1 for i, rec in enumerate(lemma_records)
        }
        if args.round is not None:
            match = next((r for r in round_ranges if r.round == args.round), None)
            if match is None:
                avail = ", ".join(str(r.round) for r in round_ranges)
                print(
                    f"Error: round {args.round} not in trace. Detected rounds: {avail}",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Slice BEFORE the standard filters so --strategy etc. operate
            # within the round's lemmas. Renumbering after filter is then
            # 1-based within the picked round.
            lemma_records = lemma_records[match.lemma_start - 1 : match.lemma_end]
            print(
                f"[round {match.round}] lemmas {match.lemma_start}-"
                f"{match.lemma_end} ({'closed' if match.closed else 'open'})",
                file=sys.stderr,
            )

    # Apply filters (affects all modes). Warn on --top-n without --top-by and
    # vice versa since they're meant to be used together.
    if args.top_n is not None and args.top_by is None:
        print("Warning: --top-n has no effect without --top-by", file=sys.stderr)
    total_before = len(lemma_records)
    lemma_records = _apply_filters(
        lemma_records,
        strategies=args.strategy,
        min_vars=args.min_vars,
        min_preconds=args.min_preconds,
        min_monomials=args.min_monomials,
        top_by=args.top_by,
        top_n=args.top_n,
    )

    # `--sample STRATEGY=N` (or its `--sample-nlsat N` shorthand) picks N
    # lemmas evenly spread across those whose strategy matches. Applied
    # AFTER the standard filters so callers can compose
    # `--strategy ord --min-vars 6 --sample ord=4` and get 4 from the
    # post-filter list.
    sample_spec = args.sample
    if args.sample_nlsat is not None:
        if sample_spec is not None:
            print(
                "Error: --sample and --sample-nlsat are mutually exclusive.",
                file=sys.stderr,
            )
            sys.exit(2)
        sample_spec = f"nlsat={args.sample_nlsat}"
    if sample_spec is not None:
        strategy_substr, n = _parse_sample_spec(sample_spec)
        before_sample = len(lemma_records)
        lemma_records = _apply_sample(lemma_records, strategy_substr, n)
        print(
            f"[sample] strategy~{strategy_substr!r} n={n}: "
            f"{len(lemma_records)}/{before_sample} lemmas picked",
            file=sys.stderr,
        )

    filtered = len(lemma_records) != total_before
    if filtered and sample_spec is None:
        print(
            f"[filter] {len(lemma_records)}/{total_before} lemmas match",
            file=sys.stderr,
        )

    fmt = args.format
    use_rich = fmt is None or fmt == "rich"
    console = make_console(no_color=args.no_color) if use_rich else None
    # `max_len=None` is the no-truncate sentinel for the render layer.
    max_len = None if args.no_truncate else _SMT_EXPR_MAX_LEN

    # Determine mode. `--sample` defaults to list output when no other
    # render mode is set (matches the workflow it's replacing — quick
    # one-liners for the picked indices). `--by-round` implies list mode
    # — it's a grouping over the same row format.
    if args.by_round:
        if args.detail is not None or args.details is not None:
            print(
                "Error: --by-round groups list output; combine with --list, "
                "not --detail/--details.",
                file=sys.stderr,
            )
            sys.exit(2)
        _render_list_by_round(
            lemma_records,
            round_ranges,
            orig_idx_by_lineno,
            fmt,
            console,
            varmap,
            max_len,
        )
    elif args.list:
        _render_list(lemma_records, fmt, console, varmap, max_len)
    elif args.detail is not None:
        _render_details([args.detail], lemma_records, fmt, console, varmap, max_len)
    elif args.details is not None:
        ranges = parse_lemma_ranges(args.details)
        indices = expand_lemma_ranges(ranges, len(lemma_records))
        _render_details(indices, lemma_records, fmt, console, varmap, max_len)
    elif sample_spec is not None:
        _render_list(lemma_records, fmt, console, varmap, max_len)
    else:
        _render_summary(
            nla_entries,
            lemma_records,
            fmt,
            console,
            varmap,
            args.limit,
            args.delta_limit,
            productivity_threshold=args.productivity_threshold,
            yield_threshold=args.yield_threshold,
            max_len=max_len,
        )


def _run_xform(args, trace_path: Path) -> None:
    """--x-form mode: stable nlsat-call fingerprints. Default source is
    per-lemma varmap snapshots (no extra capture); --x-form-source nra
    selects the older [nra]-block path which costs ~8x in trace size."""
    if getattr(args, "target_only", False):
        _run_target_only(args, trace_path)
        return

    from lemur.lemma_xform import parse_xform_calls
    from lemur.nra_parsers import (
        build_xform_report,
        render_xform_json,
        render_xform_plain,
        render_xform_rich,
    )

    if args.x_form_source == "nra":
        nra_source = Path(args.nra_trace) if args.nra_trace else trace_path
        if not nra_source.exists():
            print(f"Error: nra trace not found: {nra_source}", file=sys.stderr)
            sys.exit(1)

    coarse = bool(getattr(args, "coarse", False))
    try:
        calls, source = parse_xform_calls(
            str(trace_path),
            prefer=args.x_form_source,
            nra_trace_path=args.nra_trace,
            coarse=coarse,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not calls:
        print(
            f"No x-form fingerprintable nlsat calls in {trace_path}.\n"
            f"  - varmap path needs `~lemma_builder` + paired "
            f"`false_case_of_check_nla` entries (default `-tr:nla_solver` "
            f"output).\n"
            f"  - nra path needs `[nra] check` constraint pools "
            f"(`-tr:nra`; ~8x trace size).",
            file=sys.stderr,
        )
        sys.exit(1)

    report = build_xform_report(calls, top=args.top)
    coarse_suffix = (
        ", coarse: literals→LIT, #NNN α-renamed (structural shape)" if coarse else ""
    )
    if source == "varmap":
        unit_label = "lemmas (~lemma_builder)"
        provenance = (
            "varmap-resolved (R/I-form lemma signatures, from "
            "-tr:nla_solver). One unit = one lemma emission." + coarse_suffix
        )
    else:
        unit_label = "nlsat calls"
        provenance = (
            "nra constraint pool (x*-form, from -tr:nra). One unit = one "
            "nlsat invocation. Trace cost: ~8x larger than -tr:nla_solver."
            + coarse_suffix
        )

    show = bool(getattr(args, "show", False))
    fmt = args.format
    if fmt == "json":
        import json as _json

        body = _json.loads(render_xform_json(report, show=show))
        body["source"] = source
        body["unit"] = unit_label
        body["coarse"] = coarse
        print(_json.dumps(body, indent=2))
        return
    if fmt == "rich" or (fmt is None and sys.stdout.isatty()):
        console = make_console(no_color=args.no_color)
        render_xform_rich(report, console, unit_label=unit_label, show=show)
        console.print(f"[dim]source: {provenance}[/dim]")
        return
    sys.stdout.write(render_xform_plain(report, unit_label=unit_label, show=show))
    sys.stdout.write(f"source: {provenance}\n")


def _run_target_only(args, trace_path: Path) -> None:
    """--target-only: per-lemma target monomial fingerprint with strategy
    crosstab. Strict relaxation of --x-form --coarse; designed as a
    cascade diagnostic, not general dedup."""
    from lemur.lemma_xform import (
        build_target_report,
        parse_lemma_target_calls,
        render_target_json,
        render_target_plain,
        render_target_rich,
    )

    coarse = bool(getattr(args, "coarse", False))
    # The target text is the headline of this view; printing it in plain/
    # rich is required for the output to be readable at all. JSON still
    # honors --show so callers can ask for a compact (fingerprint-only)
    # body. Counts are usually small (~25 rows) so the verbosity is
    # cheap.
    json_show = bool(getattr(args, "show", False))
    if args.x_form_source == "nra":
        # The [nra] path doesn't carry strategy / target-var data; only
        # the varmap-resolved view supports target-only.
        print(
            "Error: --target-only requires the varmap path "
            "(--x-form-source varmap or auto, not nra).",
            file=sys.stderr,
        )
        sys.exit(2)

    calls = parse_lemma_target_calls(str(trace_path), coarse=coarse)
    if not calls:
        print(
            f"No ~lemma_builder + varmap pairs in {trace_path} for target-only view.",
            file=sys.stderr,
        )
        sys.exit(1)

    report = build_target_report(calls, top=args.top)
    coarse_suffix = " (coarse: literals→LIT, #NNN α-renamed)" if coarse else ""
    provenance = (
        "varmap-resolved target monomial per ~lemma_builder; one unit = "
        "one lemma emission" + coarse_suffix
    )

    fmt = args.format
    if fmt == "json":
        import json as _json

        body = _json.loads(render_target_json(report, show=json_show))
        body["unit"] = "lemmas (~lemma_builder)"
        body["coarse"] = coarse
        body["view"] = "target-only"
        print(_json.dumps(body, indent=2))
        return
    if fmt == "rich" or (fmt is None and sys.stdout.isatty()):
        console = make_console(no_color=args.no_color)
        render_target_rich(report, console, show=True)
        console.print(f"[dim]source: {provenance}[/dim]")
        return
    sys.stdout.write(render_target_plain(report, show=True))
    sys.stdout.write(f"source: {provenance}\n")


def _render_list(lemma_records, fmt, console, varmap, max_len=_SMT_EXPR_MAX_LEN):
    if not lemma_records:
        print("No lemmas found.", file=sys.stderr)
        return
    if console and (fmt is None or fmt == "rich"):
        render_lemma_list_rich(lemma_records, console, varmap=varmap, max_len=max_len)
    else:
        print(render_lemma_list_plain(lemma_records, varmap=varmap, max_len=max_len))


def _render_list_by_round(
    lemma_records,
    round_ranges,
    orig_idx_by_lineno,
    fmt,
    console,
    varmap,
    max_len=_SMT_EXPR_MAX_LEN,
):
    """List output sectioned by round. Empty rounds (filtered out by
    --strategy etc.) are skipped silently."""
    if not lemma_records:
        print("No lemmas found.", file=sys.stderr)
        return

    by_round: dict[int, list] = {}
    for rec in lemma_records:
        orig_idx = orig_idx_by_lineno.get(rec.entry.line_number)
        if orig_idx is None:
            continue
        for r in round_ranges:
            if r.lemma_start <= orig_idx <= r.lemma_end:
                by_round.setdefault(r.round, []).append(rec)
                break

    use_rich = console and (fmt is None or fmt == "rich")
    for r in round_ranges:
        recs = by_round.get(r.round, [])
        if not recs:
            continue
        marker = "" if r.closed else "  (open — trace ended mid-round)"
        header = (
            f"Round {r.round}: lemmas {r.lemma_start}–{r.lemma_end} "
            f"({len(recs)} after filter){marker}"
        )
        if use_rich:
            console.print()
            console.print(RichText(header, style="bold yellow"))
            render_lemma_list_rich(recs, console, varmap=varmap, max_len=max_len)
        else:
            print()
            print(header)
            print(render_lemma_list_plain(recs, varmap=varmap, max_len=max_len))


def _render_details(
    indices, lemma_records, fmt, console, varmap, max_len=_SMT_EXPR_MAX_LEN
):
    if not lemma_records:
        print("No lemmas found.", file=sys.stderr)
        return
    for idx in indices:
        i = idx - 1
        if i < 0 or i >= len(lemma_records):
            print(
                f"[warn] lemma {idx} out of range (1-{len(lemma_records)})",
                file=sys.stderr,
            )
            continue
        if console and (fmt is None or fmt == "rich"):
            if idx != indices[0]:
                console.print()
            render_lemma_detail(
                lemma_records[i], idx, console, varmap=varmap, max_len=max_len
            )
        else:
            if idx != indices[0]:
                print()
            print(
                render_lemma_detail_plain(
                    lemma_records[i], idx, varmap=varmap, max_len=max_len
                )
            )


def _productivity_rows(
    prod, productivity_threshold: float, yield_threshold: float
) -> list[tuple[str, str]]:
    """Two summary lines (productivity rate + eviction yield) with
    tentative ⚠ low warnings. Returns [] when the trace lacks status
    lines (older z3 builds), since printing zeros there would be
    misleading.

    Status share is rendered as `25.5% less / 74.5% same / 0.0% more`
    so the reader sees the full split, not just the headline `less`
    rate; some operator workflows want the `more` count even when it's
    typically zero (signals a different cascade pattern when it isn't).
    """
    if not prod.available or not prod.classified_rounds:
        return [
            (
                "Round productivity",
                "unavailable (no `sz=…m_to_refine=…` lines in trace)",
            )
        ]
    share = prod.status_share
    parts = [f"{100 * share.get(s, 0.0):.1f}% {s}" for s in ("less", "same", "more")]
    rate = prod.productivity_rate or 0.0
    rate_marker = "  ⚠ low" if rate < productivity_threshold else ""
    rows = [("Round productivity", " / ".join(parts) + rate_marker)]

    yld = prod.eviction_yield
    if yld is None:
        rows.append(("Eviction yield", "unavailable (no lemmas emitted)"))
    else:
        yld_marker = "  ⚠ low" if yld < yield_threshold else ""
        rows.append(
            (
                "Eviction yield",
                f"{yld:.3f} monomials/lemma "
                f"({prod.total_evictions} / {prod.total_lemmas})" + yld_marker,
            )
        )
    return rows


def _render_summary(
    nla_entries,
    lemma_records,
    fmt,
    console,
    varmap,
    limit,
    delta_limit,
    *,
    productivity_threshold: float = 0.35,
    yield_threshold: float = 0.40,
    max_len: int | None = _SMT_EXPR_MAX_LEN,
):
    """Show nla_solver overview: entry counts + lemma summary."""
    import re
    from collections import Counter

    from lemur.parsers import group_by_function
    from lemur.productivity import compute_productivity_stats

    by_func = group_by_function(nla_entries)

    rows = []
    rows.append(("nla_solver entries", str(len(nla_entries))))
    rows.append(("Unique functions", str(len(by_func))))

    # Check calls
    if "check" in by_func:
        check_entries = by_func["check"]
        call_nums = []
        for e in check_entries:
            m = re.search(r"calls\s*=\s*(\d+)", e.body)
            if m:
                call_nums.append(int(m.group(1)))
        if call_nums:
            rows.append(
                (
                    "Check calls",
                    f"{len(check_entries)} entries, max call# = {max(call_nums)}",
                )
            )

    # init_to_refine
    if "init_to_refine" in by_func:
        mon_counts = []
        for e in by_func["init_to_refine"]:
            m = re.search(r"(\d+)\s+mons?\s+to\s+refine", e.body)
            if m:
                mon_counts.append(int(m.group(1)))
        if mon_counts:
            mn, mx = min(mon_counts), max(mon_counts)
            avg = sum(mon_counts) / len(mon_counts)
            rows.append(
                (
                    "Monomials to refine",
                    f"min={mn} avg={avg:.1f} max={mx} (n={len(mon_counts)})",
                )
            )

    # Round productivity (m_to_refine queue-shrink rate) and eviction
    # yield (monomials evicted per emitted lemma). Per-instance
    # exploration tool — thresholds tentative; see help text.
    prod = compute_productivity_stats(nla_entries)
    rows.extend(_productivity_rows(prod, productivity_threshold, yield_threshold))

    # Lemma summary
    if lemma_records:
        rows.append(("", ""))
        rows.extend(
            lemma_summary_rows(
                lemma_records,
                lemma_limit=limit,
                delta_limit=delta_limit,
                varmap=varmap,
                max_len=max_len,
            )
        )

    # Top functions
    func_counts = Counter(e.function for e in nla_entries)
    rows.append(("", ""))
    rows.append(("Top functions", ""))
    for func, cnt in func_counts.most_common(10):
        pct = 100 * cnt / len(nla_entries)
        rows.append((f"  {func}", f"{cnt} ({pct:.1f}%)"))

    if console and (fmt is None or fmt == "rich"):
        table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
        table.add_column("Key", style="bold")
        table.add_column("Value")
        for key, value in rows:
            table.add_row(key, value)
        panel_title = RichText("nla_solver", style="bold")
        console.print(Panel(table, title=panel_title, expand=False))
    else:
        for key, value in rows:
            if key and value:
                print(f"{key}: {value}")
            elif key:
                print(key)
            elif value:
                print(f"  {value}")
