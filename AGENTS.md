# Lemur tools — for agents

Z3 trace analysis. Single `lemur` command with subcommands. Manage the environment with uv.

## setup

```
uv sync --locked
uv run lemur --help
```

Use `uv run lemur ...`, or activate `.venv` for the bare commands below.
Tests: `uv run pytest`. Build: `uv build`. Optional integrations:
`uv sync --locked --extra split`, `--extra plot`, or `--all-extras`.

## lemur sweep: run z3 across seeds/configs

```
lemur sweep BENCH.smt2 --seeds 0-15 --timeout 30 \
  --config "name: key=val key=val" \
  -j 4 -f plain --save DIR
```

- `--config` repeatable. `"name:"` for defaults
- `--trace nla_solver,nra` enables CTRACE, saves .trace files
- `--save DIR` saves .trace .stdout .stderr .z3log per run
- `--verbosity N` z3 verbose level (default 2, stderr output)
- `--z3-log` capture AST trace log (large). requires `--save`
- `-f plain` for machine output. `-f json` for structured. default auto-detects tty
- z3 binary: `~/ag/z3/z3-edge/build/z3` (debug build, supports tracing)
- shows copy-pasteable z3 commands. `--no-commands` to hide

plain output format:
```
config,seed,status,time_s
baseline,0,sat,1.234
```

## lemur export: lossless JSONL for agents

```
lemur export .z3-trace -o events.jsonl
lemur export .z3-trace --recognizer bv -o annotated.jsonl
lemur export --agent
```

Original records preserve every input byte. Optional recognizers add derived
records referencing the original event IDs. Agent help includes Sea-MLIR BV
capture commands, jq searches, and the recognizer pipeline API.

## lemur run-report: explain saved Z3 runs

`lemur run-report out/default_s0` reads sweep artifacts; `-f json` retains all
phase/progress records and source lines. Capture with `--stats --verbosity 10`.
Use `lemur run-report --agent` for schema and timer/counter semantics.

## lemur stats: analyze .z3-trace files

```
lemur stats TRACE_FILE
lemur stats TRACE_FILE --tag nla_solver
lemur stats TRACE_FILE --lemma-list
lemur stats TRACE_FILE --lemma-list -f plain
lemur stats TRACE_FILE --lemma-detail 3
lemur stats TRACE_FILE --lemma-details 1:5
lemur stats TRACE_FILE --no-varmap --lemma-detail 3
```

- parses `-------- [TAG] func file:line ---------` blocks
- `--tag TAG` filter. `--fn FUNC` filter. repeatable
- `--lemma-list` one line per lemma (strategy, conclusion, monomials)
- `--lemma-detail N` full variable table for Nth lemma (1-based)
- `--lemma-details 1:5,10` ranges
- `--no-varmap` show raw LP j-variables instead of SMT names
- `-f plain` or `-f json` for machine output

strategy short names: nlsat, pseudo-lin, grob-q, grob-f, grob-eq,
ord-binom, ord-acbd, ord-factor, mono<, mono>, tan1, tan2, tan-plane,
low>val, hi<val, sign-1mon, neutral-mon, neutral-fac, div-mono, pl-mon,
add, nex, pdd. long unknowns truncated with ~

## trace format

z3 debug build: `z3 -tr:nla_solver problem.smt2`
trace goes to `.z3-trace` in cwd. parallel runs need separate dirs (sweep handles this).

tags: `nla_solver`, `nra`, `nlsat_*`. defined in `z3-edge/src/util/trace_tags.def`.

## typical agent workflow

1. sync from the checkout: `uv sync --locked`; use `uv run` or activate `.venv`
2. sweep to find interesting seeds: `lemur sweep bench.smt2 --seeds 0-15 --timeout 30 -f plain`
3. re-run interesting case with tracing: `lemur sweep bench.smt2 --seeds 3 --timeout 60 --trace nla_solver --save ./out`
4. analyze trace: `lemur stats ./out/default_s3.trace`
5. list all lemmas: `lemur stats ./out/default_s3.trace --lemma-list -f plain`
6. inspect specific lemma: `lemur stats ./out/default_s3.trace --lemma-detail 1`

## Before committing

Use Conventional Commits (`type(scope): subject`, scope optional), with a
header of at most 72 characters; see `.commitlintrc.yml`. CI checks each
commit in the PR. Use a `style:` commit for formatting-only changes.

Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
Use `uv run ruff format .` to format code. CI repeats these checks on PRs.
