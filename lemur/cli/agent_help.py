"""
Agent-friendly usage guides. Shared by `lemur --agent` and every subcommand's
`--agent` flag.

Each subcommand has its own section. `full()` composes them all. `section(name)`
prints one focused section.
"""

import sys

WHY = """\
why reach for lemur instead of ad-hoc bash/grep/awk/python:

- trace-aware. the parser understands z3 TRACE block structure
  (header/body/footer); filters act on tag/function as structural fields,
  not substrings of bytes. grep can't distinguish a `[nla_solver]` header
  from the string "nla_solver" inside someone's body.
- orchestration with clean shutdown. `sweep` runs z3 in a process pool,
  each z3 in its own process group so Ctrl-C, timeouts, and --stop-on all
  kill every child cleanly. no orphaned z3 processes. no trace-file
  collisions between parallel runs (each run gets its own tmpdir).
- streaming + early abort. CSV rows flush as each run finishes, so you
  can pipe to a live viewer; --stop-on / --fail-fast cancel remaining
  work the instant a condition is met.
- structured records. `nla` parses lemmas into LemmaRecord (strategy,
  preconditions, conclusion, variable table, monomials) — regex alone
  doesn't get nesting right. `stats-compare` parses z3's -st S-expr into
  typed key/value pairs with side-by-side means + diff%.
- reproducible saves. `--save DIR` writes canonical files per (split,
  config, seed): .trace / .stdout / .stderr / .stats.json / .z3log.
  post-hoc analysis (tally, stats-compare, nla, search) reads the dir
  shape directly.
- case-split workflow. `--split` rewrites (check-sat) in-place, runs
  the cross-product, and reports whether the disjunction is closed. you
  don't hand-edit SMT variant files. `lemur split` goes a step further
  — it discovers candidate Boolean splits automatically, probe-scores
  them, and emits a plan.json + leaf .smt2 files that `lemur sweep DIR/`
  consumes directly.

when bash is fine: one-off `z3 ... | grep sat`. when lemur wins: more
than one seed, more than one config, or you need structured output that
another lemur command will consume.
"""

SECTIONS = {
    "sweep": """\
lemur sweep BENCH.smt2 [...] | lemur sweep LEAVES_DIR/
  why: parallel z3 pool with process-group kill on Ctrl-C / timeout /
       --stop-on (no orphaned z3 processes), per-run tmpdir isolation
       (traces don't collide), streaming CSV (rows flush as runs complete).

  decide:
    quick triage on one bench         → BENCH.smt2 --seeds 0-15 --timeout 30
    A vs B config comparison          → --config 'A: ...' --config 'B: ...'
    parameter cross-product            → --grid key=v1,v2,v3
    UNSAT-by-decomposition workflow   → --split ... or LEAVES_DIR/
                                        + --stop-on-per-split unsat --tally
    save raw outputs per run          → --save DIR
    z3 stats counters                 → --stats (with --save → .stats.json/run)
    abort on first sat/unsat          → --stop-on
    streaming, pipe-friendly CSV      → -f plain (auto-default off-tty)

  modes:
    BENCH.smt2          single benchmark; classic seeds × configs × splits.
    LEAVES_DIR/         directory mode. Reads <dir>/plan.json (written by
                        `lemur split`) and treats every non-pruned leaf as
                        an implicit split. Pruned leaves surface in the
                        tally as pre-closed UNSAT. plan.json is the
                        authoritative manifest — extra .smt2 files in the
                        directory are ignored.

  config / grid / split grammar:
    --config "name: key=val key=val"
        Named z3 config; repeatable. Quote whitespace values:
          --config 'A: smt.arith.solver=2'
          --config 'B: tactic.default_tactic="(then simplify smt)"'
    --grid "key=v1,v2,v3"
        Cross-product expansion; repeatable. Combined with --config bases.
    --split "name:<smt>"
        Inject `<smt>` before (check-sat). Repeatable. Cross-products with
        configs × seeds and adds a `split` column to CSV / tally.

  flags (input):
    BENCH | LEAVES_DIR  positional: SMT2 file or split-output directory.
    --seeds RANGE       0-15, 1,3,5, or 0-3,7. Default: 0-3.
    --timeout SECS      per-run wall-clock timeout. Default: 30.
    --z3 PATH           z3 binary. Default: ~/ag/z3/z3-edge/build/z3.
    --verbosity N       z3 -v:N. Default: 2; pass 0 to disable.

  flags (execution):
    -j N | -j auto      parallel jobs. auto = os.cpu_count(). Default: 1.
    --stop-on sat|unsat
                        abort the entire sweep on first matching status.
    --stop-on-per-split sat|unsat
                        scope --stop-on per split: close a split on its
                        first matching run, then skip its remaining runs.
                        Requires --split or directory mode. Mutually
                        exclusive with --stop-on; composes with --fail-fast.
    --fail-fast         abort on first timeout/unknown/error. Implies
                        --stop-on-error.
    --stop-on-error     abort on first run with status=error (narrower
                        than --fail-fast — leaves timeout/unknown alone).
                        Useful when a single config typo would otherwise
                        propagate across the whole sweep.

  flags (output):
    -f rich|plain|json  output format. Plain emits CSV (see schema).
                        Default: rich on tty, plain otherwise.
    --tally             append per-(split,)config aggregation. Forced on
                        whenever splits are present.
    --no-commands       suppress trailing copy-pasteable z3 command list.
    --no-color          disable color in rich output.
    --save DIR          per-run files written under DIR. Naming:
                          <config>_s<seed>.{stdout,stderr,trace,z3log,
                                            stats.json}
                        without splits, or
                          <split>.<config>_s<seed>.{ext}
                        with --split or directory mode. The .trace
                        extension is only present if --trace was set;
                        .z3log only with --z3-log; .stats.json only with
                        --stats.
    --trace TAGS        comma-separated trace tags (e.g. nla_solver,nra).
                        Captures .z3-trace; with --save writes per run.
    --z3-log            enable z3 AST trace log (trace=true). Requires --save.
    --stats             enable z3 -st; with --save writes
                        <config>_s<seed>.stats.json per run (consumed by
                        `lemur stats-compare`).
    --no-config-check   skip the pre-flight z3 dry-run that catches
                        malformed configs. The check fires once per
                        config (one z3 invocation on a trivial
                        `(set-logic QF_LIA) (assert true) (check-sat)`)
                        and aborts the sweep if any config produces an
                        `(error ...)`. Auto-skipped under -f plain on
                        the assumption plain = scripted/agent use.

  csv schema (plain output, streamed; row-per-run as runs complete):
    columns: [split,] config, seed, status, time_s
        split   present iff --split or directory mode.
        status  one of: sat | unsat | timeout | unknown | error.
        time_s  wall-clock seconds, fixed 3 decimals.

  tally schema (printed after sweep when --tally or splits are present):
    plain (CSV): [split,] config, total, sat, unsat, timeout, unknown,
                 error, fastest_sat_time_s, fastest_sat_seed,
                 fastest_unsat_time_s, fastest_unsat_seed
    json:        [ {config, total, sat, unsat, timeout, unknown, error,
                    fastest_sat:  {"time_s": F, "seed": N} | null,
                    fastest_unsat:{"time_s": F, "seed": N} | null,
                    split?: "<name>"}, ... ]

  exit codes:
    0  every scheduled run completed (regardless of sat/unsat outcome).
       --stop-on, --fail-fast, and --stop-on-error truncate the sweep
       but the process still exits 0 on a clean truncation.
    2  argparse usage error, OR a pre-flight config check failed
       (run with --no-config-check to bypass).
    other non-zero: unhandled exception (z3 not found, save-dir
       unwritable, etc.).

  performance: total wall ≈ (n_seeds × n_configs × n_splits) / jobs ×
  per-run-time. Use -j auto + --stop-on-per-split unsat to skip
  already-closed splits.

  typical session:
    # 1. seed triage
    lemur sweep bench.smt2 --seeds 0-15 --timeout 30 --tally -f plain > out.csv
    lemur tally out.csv

    # 2. UNSAT by decomposition (auto-discovered splits)
    lemur split bench.smt2 --out leaves/
    lemur sweep leaves/ --seeds 0-7 --timeout 10 \\
      --stop-on-per-split unsat --tally -f plain

    # 3. config A vs B with stats
    lemur sweep bench.smt2 --seeds 0-7 --timeout 30 --stats --save ./out \\
      --config 'A: smt.arith.solver=2' --config 'B: smt.arith.solver=6'
    lemur stats-compare ./out --top 20

  common combinations:
    --seeds 0-15 --tally -f plain
        → seed triage CSV; pipe to `lemur tally`
    --split ... --split ... --stop-on-per-split unsat --tally
        → close each disjunct independently
    --config A --config B --stats --save DIR
        → A-vs-B with z3 stats; feed to `lemur stats-compare DIR`
    --trace nla_solver --save DIR
        → produce .z3-trace for `lemur nla` / `lemur search`

  related:
    lemur tally          structured aggregation of a sweep CSV.
    lemur stats-compare  side-by-side z3 stats from --stats --save.
    lemur split          auto-discover Boolean splits → leaf SMT2 dir.
    lemur split-status   walk a recursive split tree.
    lemur search         grep over .z3-trace files.
    lemur nla            structured nla_solver lemma drill-down.
""",
    "tally": """\
lemur tally SWEEP.csv
  why: structured aggregation of a sweep CSV. counts by status AND
       tracks fastest-sat / fastest-unsat (time, seed) per config, which
       is tedious in awk. recognizes the optional `split` column from
       `sweep --split` and groups by (split, config) with a per-split
       closure summary.
  -f plain|rich|json
""",
    "stats-diff": """\
lemur stats-diff TRACE_A TRACE_B [--top-k N]
  why: side-by-side arith_conflict comparison between two traces — the
       baseline-vs-variant question that single-trace `lemur stats
       --tag arith_conflict` couldn't answer. Renders the four
       subsections (summary, hot blocks, top constants, premise shapes)
       with A / B / delta columns. Useful for "did this encoder rewrite
       shift conflict mass off BLK__65?" or "did variant B add new
       big-int constants?"

  inputs:
    TRACE_A, TRACE_B   .z3-trace files. Each must contain at least one
                       [arith_conflict] entry (errors with a clear
                       message otherwise).

  delta formats:
    counts       +N (+P%) when A>0; +N when A==0; = when equal; -N (-P%)
                 when B<A.
    percentages  premise-shape histogram uses `±Npp` (percentage points)
                 since absolute counts vary with total premise rows.

  flags:
    --top-k N    cap rows in ranked subsections (hot blocks, top
                 constants); default 5. Premise-shape histogram is
                 always 4 rows (clean_linear / ite_wrapped /
                 mod_div_wrapped / mixed).
    -f rich|plain|json   default rich for TTY, plain otherwise. JSON
                 schema mirrors the subsection structure: top-level
                 a/b/a_total/b_total + a `subsections` list of
                 {label, rows[{key, a, b, delta}]}.

  ranking rules:
    hot blocks       union of A's top-K and B's top-K, then sorted by
                     max(A_count, B_count) desc, then key for stable
                     tiebreak. Surfaces blocks that grew on either
                     side, not just A's hotlist.
    top constants    same union-then-max ordering.
    premise shapes   fixed 4-row order (clean / ite / mod_div / mixed).
""",
    "stats-compare": """\
lemur stats-compare SAVE_DIR
lemur stats-compare RUN.out [RUN.out ...] [--label NAME=GLOB]
  why: z3 -st output is an S-expression (`(:key value ...)`). this
       subcommand parses it into typed numeric dicts and shows per-config
       means side by side, with a diff % column when exactly two configs.
       "nra-calls 139 vs 22" at a glance, not via jq+awk.

  two input modes (auto-detected on the positional arg):
    sweep dir   one positional that's a dir of <config>_s<seed>.stats.json
                files (from `sweep --stats --save`).
    raw files   one or more positional files containing raw z3 -st stdout
                (a result line followed by the trailing stats S-expr). Each
                file's basename-stem becomes its config label. Use this
                when iterating manually on differently-encoded variants.

  --label NAME=GLOB   raw mode only; group multiple files under one label
                      (repeatable). GLOB is a literal path or shell glob.
                      Example: --label A='runs-A-s*.out' --label B=runs-B.out
  --top N             cap rows to N highest-magnitude stats
  -f plain|rich|json

  output: a "result" header row (sat / unsat / unknown per config) shows
  whenever inputs are raw files; below it the numeric stats sorted by
  largest magnitude (rich) or alphabetically (csv/json).
""",
    "stats": """\
lemur stats TRACE
  why: aggregates trace entries by tag and function. block-aware, so
       counts are of actual trace entries, not line occurrences.
  --tag TAG     filter to tag(s); repeatable
  --fn FUNC     filter to function(s); repeatable
  --top-k N     entries per ranked subsection (used by arith_conflict;
                default 5)
  -f plain for parsing.

  per-tag analyzers (specialized; others fall back to a generic count):
    nla_solver       function frequency, check-call max, lemma count,
                     monomials-to-refine min/avg/max
    nra              nra-result histogram (sat/unsat/...), constraint count
    arith_conflict   four subsections — summary (conflicts, distinct
                     blocks, distinct big-int constants, premise rows);
                     hot blocks (top-K BLK__ full names by # conflicts
                     containing the block — pasteable into
                     `lemur split --split-only`); top constants (top-K
                     big-int literals, with 2^N annotation when
                     recognized); premise shapes (clean_linear /
                     ite_wrapped / mod_div_wrapped / mixed % histogram).
                     Use to decide if a Boolean split is worth trying:
                     when top-2 blocks own a majority of conflicts, the
                     VC is a strong split candidate (see `lemur split
                     --split-only '^(BLK__N|BLK__M)$'`).
""",
    "nla-run": """\
lemur nla-run BENCH.smt2 [--seed N] [--timeout T] [--tactic '...']
              [--config K=V] [--z3 PATH] [--keep] [-- nla-flags...]
  why: capturing an nla_solver trace is otherwise a 3-step ritual
       (mkdir tmpdir, run z3 with -tr:nla_solver, point lemur nla at
       the resulting .z3-trace). nla-run does it in one call; the trace
       lives in a tmpdir that's cleaned up unless you pass --keep. Any
       analysis flag you'd pass to `lemur nla` flows through after `--`.

  decide:
    quick file overview              → no extra args (default summary)
    list every lemma                 → -- --list
    drill into a specific lemma      → -- --detail N
    filter by strategy               → -- --strategy SUB
    save the trace for re-analysis   → --keep (prints tmpdir path)
    custom tactic chain              → --tactic '(then simplify smt)'
    extra z3 params                  → --config smt.arith.solver=2
                                       (repeatable)

  inputs:
    BENCH.smt2          positional. The benchmark to run z3 against.
    --seed N            sat / smt / nlsat random seed. Default: 0.
    --timeout T         z3 timeout in seconds. Default: 30.
    --tactic STR        value for `tactic.default_tactic`. Quote SMT2
                        chains, e.g. '(then simplify propagate-values smt)'.
    --config K=V        extra z3 param. Repeatable. Same syntax as
                        `lemur sweep --config`'s param part.
    --z3 PATH           z3 binary. Default: ~/ag/z3/z3-edge/build/z3.
    --keep              do NOT delete the trace tmpdir on exit; print
                        its path. Use when you want to re-run `lemur nla`
                        with different flags without re-running z3.

  forwarding to `lemur nla`:
    Anything after a literal `--` separator is forwarded verbatim. So:
      lemur nla-run BENCH --seed 3 -- --list --strategy grob
    runs z3 + then `lemur nla TRACE --list --strategy grob`.

  output:
    On stderr (always):
      # z3 status: <sat|unsat|timeout|unknown|error> (T.TT s)
      # trace: <abs-path-to-trace>
      [# kept tmpdir: <path>]   (only with --keep)

    On stdout: whatever `lemur nla TRACE [forwarded-flags]` would emit.
    The exit code is `lemur nla`'s exit code (so 0 normally, 2 on
    forwarded-flag usage error). Errors before `lemur nla` runs:
       1   benchmark not found, no trace produced (z3 status logged
           on stderr).
       2   argparse usage error.

  performance: dominated by the z3 run. Use --timeout to bound it; the
  wrapper itself adds ~200 ms (one extra Python interpreter for
  `lemur nla`). For repeated analysis on the same trace, prefer:
    lemur nla-run BENCH --keep
    lemur nla <printed tmpdir>/default_s0.trace --strategy ...

  typical session:
    # 1. file overview at default settings
    lemur nla-run bench.smt2

    # 2. seed-N drill-down with a custom tactic, listing all lemmas
    lemur nla-run bench.smt2 --seed 3 --timeout 60 \\
      --tactic '(then simplify propagate-values solve-eqs smt)' \\
      -- --list

    # 3. capture once, analyze N ways
    lemur nla-run bench.smt2 --keep
    # (note printed tmpdir; trace lives at <tmpdir>/default_s0.trace)
    lemur nla <tmpdir>/default_s0.trace --top-by vars --top-n 5
    lemur nla <tmpdir>/default_s0.trace --strategy grob --details :10

  related:
    lemur nla       drill-down on an existing trace (no z3 invocation).
    lemur nla-diff  compare two traces.
    lemur sweep --trace nla_solver --save  bulk capture across seeds /
                                            configs (more flexible than
                                            nla-run for grids).
""",
    "nla-diff": """\
lemur nla-diff TRACE_A TRACE_B [--top N] [--nra-a PATH] [--nra-b PATH]
  why: Comparing two nla_solver traces is the core analysis when
       investigating "why does seed N close in 4 s and seed M time out?"
       on a Certora QF_NIA benchmark. The headline numbers (total
       nla_solver entries, lemma counts, function-call distribution,
       is_patch_blocked rate, x-form nlsat-call repeats) come out as a
       single delta table instead of a hand-built awk pipeline.

  decide:
    why is one seed slow vs another?  → nla-diff TRACE_A TRACE_B
    A and B from `sweep --trace --save` → use `<save>/<config>_s<seed>.trace`
    cap surfaced rows                 → --top N (default 5)
    scripting downstream              → --format json

  inputs:
    TRACE_A, TRACE_B   .z3-trace files. Generated by `z3 -tr:nla_solver`
                       directly or by `lemur sweep --trace nla_solver
                       --save DIR`. lemur nla-diff does NOT itself run z3.

  metrics computed (in this order; rows where both A and B are 0 are
  omitted unless they're a headline):

    total nla_solver entries          headline; tag-filter count.
    ~lemma_builder entries (lemmas)   headline; output of LemmaAnalyzer.
    check function calls              headline; group_by_function['check'].
    function: <name>                  top --top entries by max(A,B) count
                                       (excluding 'check' which is above).
    is_patch_blocked rate             count(body containing 'blocked' AND
                                       NOT 'no block') / total
                                       is_patch_blocked entries. Rendered
                                       as `N% (num/den)` for each side and
                                       a `±Npp` percentage-point delta.
    strategy: <name>                  top --top by lemma count, union of
                                       A and B's strategies.
    lemmas (varmap-resolved) /        total fingerprintable units on each
        nlsat calls (nra)             side. Label depends on the source
                                       picked by `compute_metrics`'s auto
                                       fallback: varmap (default; cheap;
                                       counts lemmas) or nra (only if no
                                       varmap data; counts nlsat
                                       invocations). `n/a` if neither.
    top-fp(i): size=K nvars=V         x-form fingerprint repeats with
        fp=<sha-prefix>                count>=2 on EITHER side, ranked
                                       by max(A, B). Fingerprint is a
                                       sha-12 hash of the sorted resolved
                                       expression set; `size` is its
                                       cardinality, `nvars` the distinct
                                       R/I (or x*) variables referenced.
                                       Replaces an older j-form
                                       fingerprint that produced 10-100x
                                       inflation due to j-ID renumbering
                                       across nlsat invocations.

  count delta format:
    +N (+P%)   when A > 0    e.g. `+200 (+312%)`
    +N         when A == 0   e.g. `+2`
    =          when A == B
    -N (-P%)   when B < A

  flags:
    --top N             surface top N entries per category. Default 5.
    --nra-a PATH        optional separately-captured -tr:nra trace for A.
                        Default: look for [nra] entries inside TRACE_A.
    --nra-b PATH        same for B.
    --format plain|json  see "json schema" below.

  json schema:
    {"a": "<abs-path>", "b": "<abs-path>",
     "rows": [{"label": "<str>", "a": <int|str>, "b": <int|str>,
               "delta": "<str>"}, ...]}
    `a` and `b` values are int for plain counts; for the
    `is_patch_blocked rate` row they're pre-formatted strings like
    "31% (20/64)".

  exit codes:
    0  success.
    1  trace file not found, parse error, or other runtime exception.
    2  argparse usage error.

  typical session:
    # 1. capture two traces (one per seed) via sweep
    lemur sweep bench.smt2 --seeds 0,3 --timeout 30 \\
      --trace nla_solver --save ./out

    # 2. headline diff
    lemur nla-diff ./out/default_s0.trace ./out/default_s3.trace

    # 3. zoom in: more strategies, more fingerprints
    lemur nla-diff ./out/default_s0.trace ./out/default_s3.trace --top 15

  related:
    lemur nla TRACE                drill into a single trace's lemma table.
    lemur search TRACE             grep over a trace's structural fields.
    lemur sweep --trace nla_solver --save  produces the trace files.
""",
    "nla": """\
lemur nla TRACE
  why: parses ~lemma_builder entries from an nla_solver trace into
       structured LemmaRecord (strategy, preconditions, conclusion,
       variable assignments with bounds/definition/root, detected
       monomials). Regex over text misses the nesting; this gives a
       typed view + filters.

  decide:
    file overview / first look       → no flags (default summary)
    one line per lemma               → --list / -l
    full table for one lemma         → --detail N (1-based, post-filter)
    full table for a range           → --details RANGE
    show raw j-vars (debug)          → --no-varmap
    keep only lemmas matching X      → --strategy / --min-* / --top-by

  modes (mutually exclusive, exactly one is active):
    (default summary)        entry counts, unique functions,
                             per-call statistics, top-N lemma previews
                             (count = --limit, default 5), and variable-
                             change deltas (count = --delta-limit,
                             default 5).
    --list, -l               one line per lemma after filters.
    --detail N               full variable + monomial table for the
                             Nth lemma. 1-based; numbers refer to the
                             post-filter list, not the original trace.
    --details RANGE          multiple details. RANGE syntax is Python-
                             slice-like: `3` (single) | `5:10` | `2-4` |
                             `:5` (head) | `12:` (tail).
    --x-form                 stable nlsat-related fingerprints. Reports
                             total fingerprintable units, unique-
                             fingerprint count, top repeats with size +
                             variable list, and size distribution. Two
                             sources, picked via --x-form-source:

                             varmap (default; --x-form-source auto/varmap):
                                Per-lemma varmap snapshot from
                                `-tr:nla_solver`. One unit = one lemma
                                (~lemma_builder emission). j-vars are
                                resolved through the lemma's own varmap to
                                stable R/I IDs (z3 enode IDs, not
                                recycled). No extra capture cost. Detects
                                duplicate lemma emissions — e.g. "lemma
                                X fired 24 times" on a stuck QF_NIA target.

                             nra (--x-form-source nra):
                                `[nra] check` constraint pool from
                                `-tr:nra`. One unit = one nlsat
                                invocation. Stable but EXPENSIVE: ~8x
                                trace size on hard benchmarks. Detects
                                duplicate nlsat questions (the
                                constraint set passed in). Use only when
                                you specifically want call-level rather
                                than lemma-level repeat detection.

                             --x-form-source auto picks varmap when
                             ~lemma_builder + paired varmap entries are
                             present; falls back to nra. --top N caps
                             repeat rows (default 10). --nra-trace PATH
                             points at a separately-captured nra trace.

  filters (compose with AND; renumbering after filtering is 1-based):
    --strategy SUB        keep lemmas whose strategy contains SUB,
                          case-insensitive. Repeatable: matches ANY
                          (substring OR over the supplied list).
    --min-vars N          keep lemmas with ≥ N variables.
    --min-preconds N      keep lemmas with ≥ N preconditions.
    --min-monomials N     keep lemmas with ≥ N detected monomials.
    --top-by FIELD        sort descending by FIELD ∈ {vars, preconds,
                          monomials}. Use with --top-n.
    --top-n N             after --top-by, keep only top N.
    --sample STRATEGY=N   pick N lemmas evenly spread across those whose
                          strategy contains STRATEGY (case-insensitive
                          substring; same matcher as --strategy). Picks
                          at indices `i * total / N` for i in [0, N) so
                          you get a representative spread of the run, not
                          just the head. Composes with the other filters
                          (`--strategy lin --min-vars 6 --sample lin=4`
                          picks 4 from the post-filter list). When no
                          render mode is set, defaults to --list output
                          for the picked rows; `--detail`/`--details RANGE`
                          override.
    --sample-nlsat N      shorthand for --sample nlsat=N.

  round-aware filters (mutually exclusive; both need the same trace tags
  as `lemur n-over-time` — capture via `lemur sweep --trace
  nla_solver,decide,pop_scope,arith_conflict --save DIR/`):
    --round N             keep only lemmas in round N (1-based). Slices
                          the lemma list BEFORE other filters, so
                          `--round 5 --strategy grob` means "grob lemmas
                          in round 5". Round 1 = first ~lemma_builder
                          through the first round-ending pop.
    --by-round            group --list output by round, with a header
                          per round listing its lemma index range and
                          whether the round closed before the trace
                          ended ("open — trace ended mid-round" tag on
                          unclosed final rounds). Empty rounds (filtered
                          out by --strategy etc.) are skipped silently.
                          Implies --list rendering — combine with
                          --strategy / --min-vars / etc., not with
                          --detail/--details.

  output flags:
    -f rich|plain         rich on TTY, plain otherwise. ⚠ The argparse
                          choice `json` is currently a no-op (falls
                          through to plain) — known limitation; treat
                          plain as the canonical machine-readable form.
    --no-color            disable color in rich.
    --no-varmap           show raw LP j-variables (j7, j143) instead
                          of resolved SMT names.
    --no-truncate         show full SMT names in --detail/--details
                          and --list. Default truncates substituted
                          SMT expressions to 40 chars with trailing
                          `...` so list rows stay one line; pass this
                          when a detail row shows `...` on lemma
                          literals and you need the full text.
    --limit N             summary mode: number of lemma previews
                          (default 5).
    --delta-limit N       summary mode: max variable-change lines
                          (default 5).

  exit codes:
    0  success (including the case where filters exclude every lemma —
       a `No lemmas found.` informational message goes to stderr).
    2  argparse usage error.
    other non-zero: unhandled exception (trace parse failure, etc.).

  typical session:
    # 1. file overview
    lemur nla ./out/A_s3.trace

    # 2. list every grobner-strategy lemma
    lemur nla ./out/A_s3.trace --strategy grob --list

    # 3. top-5 widest lemmas (most variables)
    lemur nla ./out/A_s3.trace --top-by vars --top-n 5

    # 4. detail on the 3rd lemma after filtering
    lemur nla ./out/A_s3.trace --strategy grob --detail 3

  related:
    lemur sweep --trace nla_solver --save  produces the trace files.
    lemur search                           grep / filter the same trace.
    lemur stats                            tag/function aggregates.
""",
    "export": r"""lemur export TRACE [-o EVENTS.jsonl] [--recognizer bv] [--format jsonl]

  purpose: preserve a .z3-trace as an ordered JSONL event stream. Query it
  with jq/Python; layer domain recognizers over it. No Z3 Python dependency.
  TRACE=- reads stdin; output defaults to stdout. --agent needs no TRACE.

  schema v1:
    kind=trace: seq, tag, function, source_file/source_line, complete,
                span, text; incomplete records have reason=eof|next-header.
    kind=raw: seq, span, text. Unframed text has no guessed tag (STRACE
              does not carry one). Blank lines are also preserved.
    span: byte_start/byte_end (zero-based, end exclusive), line_start/
          line_end (one-based, inclusive) in the original trace.
    text: exact input including framing/newlines, JSON escaped. Surrogate
          escapes preserve invalid UTF-8 bytes. No truncation or humanization.
    seq: one-based, includes raw fragments, local to this input file.
    Memory: one block or contiguous unframed run; no whole-file load.

  --recognizer bv adds kind=recognition records, with recognizer/version,
  event_ids, and data. Original records pass through unchanged. v1 knows:
    bitblast_application: operator printed by bit_blaster/reduce_app
    bv_internalize: expr_id and printed expression from theory_bv.cpp
    sat_clause: learned|aux role and literal text at mk_clause_core
    sat_decision: decision level and literal text
  Recognition is conservative, source-site/body-shape dependent, and skips
  incomplete events. No match means unknown, not absence of solver activity.
  Printed expressions need not be valid SMT2; IDs are local to a solver run.
  Clause records are creation attempts BEFORE simplification, not retained
  clause counts. Bit-blaster events are visits, not cost or unique terms.

  examples (run from the Lemur checkout; jq is optional):
    uv run lemur export .z3-trace -o events.jsonl
    uv run lemur export .z3-trace --recognizer bv -o annotated.jsonl
    jq -c 'select(.kind == "trace" and .tag == "bv")' events.jsonl
    jq -c 'select(.kind == "recognition") | .data' annotated.jsonl
    jq -r 'select(.seq == 12) | .text' events.jsonl
    # Recover exact input bytes, including invalid UTF-8 and final fragments:
    python -c 'import json,sys
for line in sys.stdin:
    r = json.loads(line)
    if r["kind"] in ("trace", "raw"):
        sys.stdout.buffer.write(r["text"].encode("utf-8", "surrogateescape"))' < annotated.jsonl > recovered.trace

  Sea-MLIR / BV exploration (set SEA_MLIR, SEA_VC, Z3 to your checkouts/tools):
    # stdout is SMT2; diagnostics stay on stderr. Keep the exported query.
    "$SEA_VC" "$SEA_MLIR/test/VC/mul-overflow.mlir" --smt2 > query.smt2
    # Or export the larger application case; its assumptions remain in force:
    "$SEA_VC" "$SEA_MLIR/test/VC/seam/Inputs/nki-scatter-K4-int8.mlir" --lower --seam --smt2 > scatter.smt2
    # Every capture needs its own working directory: Z3 writes .z3-trace there.
    mkdir -p capture
    cp query.smt2 capture/
    (cd capture && "$Z3" -version > solver-version.txt && "$Z3" -T:5 -tr:before_bit_blaster -tr:after_bit_blaster -tr:bit_blaster -tr:sat_decide -tr:sat query.smt2 > result.txt)
    uv run lemur export capture/.z3-trace --recognizer bv -o capture/events.jsonl

  choosing tags (trace-enabled Z3 required):
    before_bit_blaster / after_bit_blaster: goal snapshots; useful baseline.
    bit_blaster: applications visited during blasting; can be very verbose.
    sat_decide: decisions in the SAT engine. sat: clauses plus much more.
    bv: BV-theory internalization/propagation, especially SMT/array queries.
    bv_eq / bv_bit_prop: selected SMT BV equality/bit propagation sites.
  These cover different engines. If a pipeline never reaches a stage, its
  tags stay silent. SMT decide/pop_scope tags differ from SAT sat_decide.
  Trace timeouts and volume are instrumented runs, not solver benchmarks.
  Keep the solver version, command/options and original SMT2 beside the trace.

  layering in Python:
    from lemur.trace_recognizers import recognize, BVRecognizer
    # Loaded JSONL evidence can go through a recognizer without reparsing text:
    events = (json.loads(line) for line in source)
    evidence = (r for r in events if r["kind"] in ("trace", "raw"))
    annotated = recognize(evidence, [BVRecognizer()])
  A recognizer implements feed(event) and finish(), yielding dictionaries with
  event_ids and data. It may retain state and refer to multiple input events.
  finish() can emit incomplete matches. Raw framing and existing NLA report
  commands are independent; a future NLA recognizer can reuse their domain logic.
""",
    "search": """\
lemur search TRACE [PATTERN] [--tag RE] [--fn RE] [-n] [--entries]
  why: trace-aware grep. Z3 TRACE files have a block structure
       (header / body / footer). --tag '^nla' matches every
       [nla_*] entry HEADER structurally, not just any line that
       happens to contain "nla". Line numbers are absolute into the
       file (editor-jumpable). --entries prints whole blocks for
       context.

  decide:
    just match a body-line pattern    → search TRACE PATTERN
    filter to specific tag(s)         → --tag REGEX
    filter to specific function(s)    → --fn REGEX
    dump every line in matching tags  → omit PATTERN (keep --tag/--fn)
    count without printing            → -c
    cap output at N matches           → --max-count N
    invert match                      → -v
    case-insensitive                  → -i
    print full entry blocks           → --entries
    line numbers for editor jump      → -n

  inputs:
    TRACE        path to .z3-trace file (from `lemur sweep --trace`).
    PATTERN      optional regex (re.search semantics) over body lines.
                 Omit to dump every line in entries passing tag/fn.
    --tag RE     re.search against entry tag (the bracketed token in
                 `-------- [TAG] fn file:line --------`). Anchor with
                 ^/$ as needed: `--tag '^nla'` matches `[nla_solver]`,
                 `[nla_grobner]`, etc.
    --fn RE      re.search against entry function name.

  output formats:
    default                    one matching body line per row.
    -n / --line-number         prefix every line with `<abs_lineno>:`
                               (header lines too, when --entries).
    --entries                  each matching block printed as:
                                 -------- [tag] fn file:line --------
                                 body-line
                                 body-line
                                 ...
                                 ------------------------------------------------
    -c / --count               without --entries: print N (line count).
                               with --entries: print `N entries, M lines`.

  rich output (TTY default) highlights matched substrings; -f plain
  disables color and prints the same content without ANSI.

  exit codes:
    0  ≥ 1 match printed/counted.
    1  no match (grep convention).
    2  invalid regex in PATTERN, --tag, or --fn (or argparse usage error).

  typical session:
    # nla_solver entries containing 'calls' inside the `check` function
    lemur search ./out/A_s3.trace 'calls' --fn '^check$' --entries -n

    # count nla_solver entries
    lemur search ./out/A_s3.trace --tag '^nla_solver$' -c

    # dump every line in any [nla_*] entry, no pattern filter
    lemur search ./out/A_s3.trace --tag '^nla'

  related:
    lemur stats   tag/function aggregates over a trace.
    lemur nla     structured nla_solver lemma drill-down.
    lemur sweep --trace nla_solver --save  produces the trace files.
""",
    "split": """\
lemur split BENCH.smt2 [--out DIR] [--max-leaves N]
  why: automatically discover good Boolean case-splits on hard SMT2 (the
       kind that manually cracks 60s-TO Certora VCs). enumerates
       candidate Bool predicates (--split-name-pattern, default BLK__\\d+,
       plus ITE guards), probe-scores each via simplification, greedy-
       nests up to log2(--max-leaves). emits a self-contained output dir
       with plan.json + leaf_*.smt2 files + a verbatim copy of the
       source. optional [split] extra: run `uv sync --extra split` in the checkout.

  key flags:
    --out DIR                 default: <BENCH-stem>_children/ next to source
    --max-leaves N            cap 2^k (default 32, floor 8)
    --split-score-threshold F (default 10)
    --split-probe-timeout S   per-candidate simplify timeout (default 5s)
    --split-name-pattern RE   reachability Bool regex (default BLK__\\d+)
    --plan-only               write plan.json only; no leaf files on disk
    --force                   overwrite an --out dir that already has plan.json

  version note: the z3 Python API used here can differ from the z3
  binary that `lemur sweep` invokes. That is fine: split only
  manipulates formulas. The solver of record is the binary.
""",
    "split-status": """\
lemur split-status DIR
  why: walks a recursive split tree (from `lemur split` then recursive
       re-splits) and reports aggregate stats: total leaves across the
       tree, emitted vs pruned, max depth, per-plan summary, and whether
       each plan has sweep results populated. Lets you see at a glance
       whether the whole decomposition closed.
  -v   list every leaf with its path and state
  -f plain|rich|json    json emits the full tree for scripting
""",
    "sgrep": """\
lemur sgrep FILE.smt2 [PATTERN] [--apply TACTIC]
  why: grep is line-oriented; SMT2 has multi-line `(let …)` nesting and
       shape variants like `(div (ite c A B) k)` vs `(ite c (div A k)
       (div B k))` that read identically to a regex. sgrep walks the
       post-parser z3 AST (sharing-aware; the printer may still elide
       deeply-shared subtrees with `...` — use --expand-aliases to
       inline). Run preprocessing first via --apply to see what the
       solver actually sees.

  decide:
    no PATTERN given        → --summary (file overview)
    just want a number      → --count
    classifying many guards → --distinct --show kind
    extracting capture vals → --distinct --show captures
    scripting downstream    → --format json
    iterating on patterns   → --check-pattern (validates without running)

  modes (mutually exclusive; default: summary if no PATTERN, list otherwise):
    --summary       file overview: asserts, decls-by-sort, top operators
                    (filtered to arity > 0), distinct-shape counts (see
                    "summary shape counts" below), max nesting depth.
    --count         number of matches; exit.
    --list          one match per line.
    --distinct      --list with structurally-equal duplicates removed
                    (deduped by str(match.expr) — exact AST identity).

  pattern syntax (note: pattern uses SMT-LIB head names — `ite`, `=`,
  `+`, `*`, `div`, `mod`; --show kind output uses z3-printer names —
  `Op(if)`, `Op(=)`):

    _                    wildcard
    ?name                capture (same name twice ⇒ id-equality unification)
    (head c1 c2 ...)     compound: head op-name + arity-matched children
    NAME                 bare literal name: matches a 0-arity expr with
                         that decl name. ⚠ define-fun macros (e.g.
                         `(define-fun POW2_64 () Int 18446744073709551616)`)
                         are inlined at parse time, so `(mod _ POW2_64)`
                         matches NOTHING in such files. To match the
                         underlying value, use the literal directly:
                         `(mod _ 18446744073709551616)`. NAME works for
                         names introduced by `declare-const` /
                         `declare-fun`, which survive parsing.
    NUMERAL              integer literal; matches by value against
                         IntVal / BV-value / rational-value AST nodes.

    type filters (independent dimensions; not mutually exclusive — an
    atomic Bool free constant matches BOTH :Bool AND :Var):
      ?c:Bool         expr with Bool sort (atomic OR compound Bool)
      ?n:Var          0-arity uninterpreted constant (free variable)
      ?k:Numeral      integer / bit-vector / rational literal
      ?c:Eq           top-op `=`  (Z3_OP_EQ)
      ?c:Comparison   top-op `<` / `<=` / `>` / `>=`
      ?e:Expr         no filter (default; explicit form)
    negation: !?n:Numeral  ≡  ?n:!Numeral  (XOR — both forms together cancel)

  summary shape counts (always reported by --summary):
    (div ?a ?b)              (mod ?a ?b)            (ite ?c ?a ?b)
    (div (ite ?c ?a ?b) ?k)  (mod (ite ?c ?a ?b) ?k)
    (* ?x (ite ?c ?a ?b))    (* (ite ?c ?a ?b) ?x)

  flags:
    --apply 'TACTIC'      pre-process via z3 tactic. Grammar (v1):
                          a single tactic name OR `(then t1 t2 ...)`.
                          Rule of thumb: same tactic chain z3 itself
                          would run; expect 1–10 s on Certora-sized
                          goals (hundreds of asserts, max depth 50+).
    --show captures       per-match: append `?name=full-binding`. ⚠
                          Foot-gun: a single compound capture can dump
                          hundreds of lines per match (a 3-distinct-match
                          query produced 403 lines in one real session).
                          Prefer --show kind first; reach for captures
                          only after confirming captures are atomic.
    --show kind           per-match: ONE-LINE kind summary —
                          Var(name) / Numeral(N) / Op(head) — for the
                          match and each capture.
    --check-pattern       validate PATTERN syntax and exit; skip file
                          I/O and --apply entirely.
    --format plain|json   see "json schema" below.
    --expand-aliases      inline z3 let-aliases in printed output.
                          Beware exponential blowup on deeply-shared
                          subterms.

  json schema:
    --count    {"count": N}
    --list,
    --distinct one record per line. Default:
                 {"expr": "<str>",
                  "captures"?: {"<name>": "<str>", ...}}
               With --show kind:
                 {"kind": "<str>",
                  "captures"?: {"<name>": "<kind-str>", ...}}
    --summary  {"asserts": N,
                "decls_by_sort": {"<sort>": N, ...},
                "top_ops": {"<op>": N, ...},
                "shape_counts": {"<pattern>": N, ...},
                "max_depth": N}

  exit codes:
    0  success (regardless of match count)
    1  runtime error: file not found, SMT2 parse failure, tactic apply
                      raised z3 exception
    2  usage / parse error: bad pattern, bad tactic syntax, conflicting
                            flags, argparse-level errors

  typical session:
    # 1. quick read on a new file
    lemur sgrep FILE.smt2

    # 2. what does the solver actually see?
    lemur sgrep FILE.smt2 \\
      --apply '(then simplify propagate-values solve-eqs)'

    # 3. extract atomic-Bool guards (split candidates)
    lemur sgrep FILE.smt2 \\
      --apply '(then simplify propagate-values solve-eqs)' \\
      '(div (ite ?c:Var _ _) _)' --distinct --show captures

  common combinations:
    --apply 'CHAIN' --summary
        → "what does the solver see after preprocessing?"
    '(op (ite ?c:Var _ _) _)' --distinct --show captures
        → "extract distinct atomic-Bool guards under this operator"
    '(op (ite ?c _ _) _)' --distinct --show kind
        → "classify guards by kind without dumping subtrees"
    '(op (ite ?c:Eq _ _) _)' --distinct
        → "find ITEs guarded by an equality predicate"
    --apply 'CHAIN' --format json
        → "scriptable output of post-preprocessing structure"

  related:
    lemur sdiff --agent  — structural diff between two SMT2 files
                           (composes sgrep's pattern syntax).
""",
    "n-over-time": """\
lemur n-over-time TRACE [TRACE ...] [--label LABEL ...]
                  [--format table|json|html|png] [--out PATH] [--shared-y]
  why: visualizes the SAT/NLA/nlsat fixpoint as N(t). Each NLA emission
       is one (x=emission_index, y=current_decision_level) point. Lets
       you see whether a TO seed is escalating in N or thrashing in a
       band, and pair each round-ending pop visually back to the NLA
       emission that opened the round. The same parser also feeds a
       round-stats summary (round counts + climb / end-drop / round-
       length quartiles) — `--format table` is that view.

  decide:
    file overview / summary stats     → --format table  (default on TTY)
    feed downstream tooling           → --format json   (default off-TTY)
    interactive zoom / pan / hover    → --format html --out X.html
    static image for slides / docs    → --format png  --out X.png
    multiple seeds side-by-side       → pass multiple TRACE positionals
    custom names per subplot          → --label foo --label bar (paired)

  required trace tags (capture once, all four together):
    -tr:decide -tr:pop_scope -tr:nla_solver -tr:arith_conflict

    via lemur:  lemur sweep --trace nla_solver,decide,pop_scope,arith_conflict
                            --save DIR/
    via z3:     z3 -tr:decide -tr:pop_scope -tr:nla_solver \\
                   -tr:arith_conflict ...

    NOTE -tr:nla_solver alone adds ~5x runtime overhead on hard
    benchmarks. The four together don't add much beyond that.

  inputs:
    TRACE              positional, repeatable. Each is one .z3-trace file.
    --label LABEL      paired-by-position label per TRACE. Default: file
                       stem. Repeatable.
    --format FMT       table | json | html | png. Default: table on TTY,
                       json otherwise. html/png require --out.
    --out PATH         Output file. Required for html/png; optional for
                       table (writes CSV to file) and json.
    --shared-y         Plot formats only: shared y-axis across subplots.
                       Useful when comparing N magnitudes between traces.
    --lemmas-per-round With --format json only: augment per-trace dict
                       with a `rounds` array — one entry per detected
                       round with {round, lemma_start, lemma_end, closed,
                       lemmas: [{index, strategy, conclusion, preconds,
                       vars, monomials}]}. lemma indices match
                       `lemur nla --list` 1:1, so a downstream tool can
                       cross-reference into other lemur views.
    --no-color         Rich table only.

  what is a "round":
    A round begins at an NLA emission whose level becomes its anchor N
    (after either trace start or a prior round-ending pop). N climbs as
    NLA lemmas drive new SAT decisions. The round ends when a pop_scope
    brings the trail level *below* N. The crossing depth is N - target.

  plot semantics (html/png):
    line              N at each NLA emission (x=emission index)
    gold band         round span (start → matching pop)
    red ▼             post-POP level after a round-ending pop
    dotted gray       crossing-depth segment from prev N down to post-POP

    A trailing gold band with no closing red ▼ would indicate a round
    that didn't close before the trace ended; in practice the renderer
    only draws bands for matched (start, pop) pairs.

  table / csv schema:
    rich (TTY default): one panel per trace with totals + quartile table
                        for round_climbs, end_drops, internal_pop_drops,
                        round_lengths, n_series.
    csv (--out / piped): header columns
                          label,metric,value
                         then
                          label,metric,n,min,q1,median,q3,max,mean

  json schema (per trace, keyed by label):
    {<label>: {totals: {...}, max_decision_level: int,
               n_series: [int, ...],
               round_starts: [[idx, N], ...],
               pop_marks: [[idx, prev_N, post], ...],
               round_climbs / end_drops / internal_pop_drops /
                   round_lengths: [int, ...],
               quartiles: {round_climbs: {n,min,q1,...}, ...}}}

    Pairing: round_starts[i] is the start of the round whose end is
    pop_marks[i]. If len(round_starts) == len(pop_marks)+1, the trailing
    round didn't close before the trace ended.

  exit codes:
    0  success.
    1  trace not found, or plotly/matplotlib missing for html/png.
    2  argparse usage error (--label count mismatch, --out missing for
       html/png, etc.).

  install (for plot formats):
    uv sync --extra plot        # adds plotly, matplotlib in this checkout

  typical session:
    # 1. capture traces with the four tags
    lemur sweep bench.smt2 --seeds 0,1 --timeout 30 \\
      --trace nla_solver,decide,pop_scope,arith_conflict --save ./out

    # 2. quick stats summary
    lemur n-over-time ./out/default_s0.trace ./out/default_s1.trace

    # 3. interactive zoom on the same data
    lemur n-over-time ./out/default_s0.trace ./out/default_s1.trace \\
      --label 'TO seed' --label 'closing seed' \\
      --format html --out /tmp/n.html

    # 4. drill into the lemmas of one round visible on the plot
    lemur nla ./out/default_s0.trace --round 5 --list

    # 5. eyeball the full round-by-round breakdown
    lemur nla ./out/default_s0.trace --by-round --list

    # 6. dump round + lemma metadata for a downstream script
    lemur n-over-time ./out/default_s0.trace --format json \\
      --lemmas-per-round > rounds.json

  related:
    lemur sweep --trace ...   produces the trace files.
    lemur stats               trace-wide tag/function aggregates.
    lemur nla --round N       lemmas in one specific round (same trace,
                              lemma indices are 1:1 with this view).
    lemur nla --by-round      grouped lemma list with round headers.
""",
    "sdiff": """\
lemur sdiff A.smt2 B.smt2 [--apply TACTIC | --apply-a T --apply-b T]
  why: structural-count diff between two SMT2 files. Tells you "encoder
       Pattern-3 went from 39 occurrences to 0" without staring at
       unified diffs of mangled SMT2. Same shape table as
       `sgrep --summary`, run on both files, with A / B / delta columns.

  decide:
    A and B are different files       → A.smt2 B.smt2  +  --apply
    same source, two preprocessing
      pipelines (the self-diff trick) → A.smt2 A.smt2  +  --apply-a/-b
    only care about one pattern       → --pattern PATTERN
    surface unchanged rows too        → --show-same
    scripting downstream              → --format json

  modes:
    default          full shape table. Rows: `asserts`,
                     `declarations (Sort)` per Sort, every shape from
                     sgrep's "summary shape counts", `max nesting depth`.
    --pattern PAT    restrict to one user-supplied sgrep-style pattern.

  flags:
    --apply 'TACTIC'      symmetric: apply same tactic to both A and B.
    --apply-a 'TACTIC'    asymmetric: tactic only for A.
    --apply-b 'TACTIC'    asymmetric: tactic only for B.
                          --apply is mutually exclusive with --apply-a/-b.
                          The same-source self-diff trick:
                            lemur sdiff F.smt2 F.smt2 \\
                              --apply-a 'simplify' \\
                              --apply-b '(then simplify propagate-values)'
                          tells you what the second pipeline added.
    --show-same           include rows where A == B (default: hide).
    --format plain|json   see "json schema" below.
    --expand-aliases      same as sgrep.

  json schema:
    {"a": "<path>", "b": "<path>",
     "rows": [{"shape": "<str>", "a": N, "b": N, "delta": N}, ...]}
    delta = b - a; positive ⇒ B has more.

  exit codes:
    0  success
    1  runtime error (file not found, SMT2 parse failure, tactic apply
                      raised z3 exception)
    2  usage / parse error (bad pattern, bad tactic, conflicting --apply
                            flags, argparse-level errors)

  typical session:
    # A and B are different files (e.g. hard leaf vs trivial)
    lemur sdiff hard.smt2 trivial.smt2 \\
      --apply '(then simplify propagate-values solve-eqs)'

    # Same source, two pipelines: what did solve-eqs introduce?
    lemur sdiff bench.smt2 bench.smt2 \\
      --apply-a '(then simplify propagate-values)' \\
      --apply-b '(then simplify propagate-values solve-eqs)'

  related:
    lemur sgrep --agent  — pattern syntax, tactic grammar, --check-pattern.
""",
}


WORKFLOWS = """\
workflows:

# seed triage + per-config aggregation
  lemur sweep bench.smt2 --seeds 0-15 --timeout 30 --tally -f plain > out.csv
  lemur tally out.csv

# case-split UNSAT-proof by decomposition (close every split independently)
# --stop-on-per-split unsat: each split runs until one seed UNSATs, then skips
# remaining seeds for that split only; other splits keep running. tally shows
# whether the disjunction is fully closed.
  lemur sweep bench.smt2 --seeds 0-15 --timeout 30 --split 'BLK25:(assert BLK__25)' --split 'BLK26:(assert BLK__26)' --stop-on-per-split unsat --tally -f plain

# auto-discover case-splits, then sweep them (recursive-ready)
# `lemur split` emits leaves + plan.json; sweep DIR/ uses plan.json as
# manifest. If a leaf is still hard, re-run `lemur split` on it.
  lemur split bench.smt2 --out leaves/
  lemur sweep leaves/ --seeds 0-7 --timeout 10 --stop-on-per-split unsat --tally -f plain
  # recurse on a stubborn leaf:
  lemur split leaves/leaf_T_F_T.smt2
  lemur split-status leaves/

# config A vs B on stats counters
  lemur sweep bench.smt2 --seeds 0-7 --timeout 30 --stats --save ./out --config 'A: smt.arith.solver=2' --config 'B: smt.arith.solver=6'
  lemur stats-compare ./out --top 20

# nla lemma drill-down
  lemur sweep bench.smt2 --seeds 3 --timeout 60 --trace nla_solver --save ./out
  lemur nla ./out/default_s3.trace --strategy grob --list
  lemur nla ./out/default_s3.trace --top-by vars --top-n 5
  lemur search ./out/default_s3.trace 'calls' --fn '^check$' --entries -n

# structural inspection of an unfamiliar VC, then compare two preprocessing
# pipelines to see what actually changed at the AST level
  lemur sgrep bench.smt2 --summary
  lemur sgrep bench.smt2 --apply '(then simplify propagate-values solve-eqs)' \\
    '(div (ite ?c _ _) _)' --distinct --show captures   # find guards
  lemur sdiff before.smt2 after.smt2 \\
    --apply '(then simplify propagate-values solve-eqs)'
"""


def full() -> str:
    parts = [
        f"lemur: z3 trace + SMT2 analysis toolkit. {len(SECTIONS)} subcommands.",
        "",
        WHY,
        "",
    ]
    for name, body in SECTIONS.items():
        parts.append(body)
    parts.append(WORKFLOWS)
    parts.append("use --help (or --agent) on any subcommand for details.")
    return "\n".join(parts)


def section(name: str) -> str:
    body = SECTIONS.get(name, "")
    return (
        body
        + "\n"
        + "for the full toolkit (all subcommands + rationale + workflows),\n"
        + "run `lemur --agent`.\n"
    )


def add_agent_flag(parser, subcommand_name: str) -> None:
    """Attach `--agent` to a subparser. The handler must be invoked before
    required-arg validation runs, so we short-circuit via a custom Action."""

    import argparse

    class _AgentAction(argparse.Action):
        def __init__(self, option_strings, dest, **kwargs):
            super().__init__(
                option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs
            )

        def __call__(self, parser, namespace, values, option_string=None):
            sys.stdout.write(section(subcommand_name))
            parser.exit(0)

    parser.add_argument(
        "--agent",
        action=_AgentAction,
        help="Show agent-friendly usage guide for this subcommand",
    )
