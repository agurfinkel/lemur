# 🐒 Lemur — Z3 Trace Analysis & Debugging Toolkit

*Like lemma, but with better eyesight* 👀

Tools for analyzing Z3 trace logs, running parameter sweeps, and comparing
solver behavior across configurations. Pure Python, no Z3 dependency — just
text parsing.

## 🌴 Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
run these commands from the checkout (Python 3.11 or newer):

```bash
uv sync --locked
uv run lemur --help
uv run pytest
```

`uv sync` creates `.venv`, installs Lemur in editable mode, and includes the
`dev` dependency group. The committed `uv.lock` pins dependencies; `--locked`
checks that it agrees with `pyproject.toml`. After changing dependencies, run
`uv lock` and commit both files.

Use `uv run lemur ...` without activating an environment, or run
`source .venv/bin/activate` to use the bare `lemur` commands below.
Optional integrations remain extras:

```bash
uv sync --locked --extra split  # Z3 Python bindings
uv sync --locked --extra plot   # Plotly and Matplotlib
uv sync --locked --all-extras   # Both integrations
uv build                       # Source distribution and wheel in dist/
```

Use `uv sync --locked --no-dev` for a runtime-only environment. The default
installation does not require Z3's Python bindings; running solver sweeps still
requires a separate Z3 executable.

## Development checks

Pull requests and pushes to `main` run Ruff lint/import checks, Ruff formatting, and
pytest on Python 3.11 and 3.14 with all optional extras. CI also builds the
wheel and source archive.

Run the checks locally:

```bash
uv sync --locked --all-extras
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
```

Commits follow Conventional Commits, using the same commitlint rules as
Sea-MLIR: `type(scope): subject` (scope optional), at most 72 characters in
the header. Allowed types are `build`, `chore`, `ci`, `docs`, `feat`, `fix`,
`perf`, `refactor`, `revert`, `style`, and `test`. Subject case and body/footer
line lengths are unrestricted. CI checks every commit in a PR, not just its
title. Example: `fix(parser): handle empty trace blocks`.

Use `uv run ruff check --fix .` and `uv run ruff format .` to apply safe
lint fixes and formatting before committing.

## Trace event export

`uv run lemur export .z3-trace -o events.jsonl` preserves trace blocks and
unframed/incomplete fragments as JSONL. Add `--recognizer bv` for optional
BV/SAT annotations linked to the original event IDs. Existing NLA reports
remain available.

Run **`uv run lemur export --agent`** for the schema, querying examples,
Sea-MLIR SMT2 generation and BV trace-tag selection. A small exported query
is in `examples/sea-mlir/mul-overflow.smt2`.

## 🔧 Tools

### 🌀 lemur sweep — Seed/config sweep runner

Run Z3 on a benchmark across seeds and configurations, collect results in a
table. Each run uses an isolated temp directory for trace file safety.

```bash
# Basic sweep
lemur sweep problem.smt2 --seeds 0-15 --timeout 30

# Multiple configs, parallel, with trace capture
lemur sweep problem.smt2 --seeds 0-15 --timeout 30 \
  --config "baseline:" \
  --config "inc1: smt.arith.nl.nra_incremental=1" \
  --config "inc2: smt.arith.nl.nra_incremental=2 smt.arith.nl.nra_max_conflicts=1000" \
  --trace nla_solver,nra \
  --save ./sweep_results \
  -j 4
```

Config format: `"name: key=val key=val"` or `"name:"` for defaults.
Quoted names work: `"\"mode 1\": key=val"`.

Output includes copy-pasteable z3 command lines for manual re-run
(suppress with `--no-commands`).

**Saved files** (with `--save DIR`):
| File | Contents |
|------|----------|
| `config_sN.trace` | `.z3-trace` file (requires `--trace`) |
| `config_sN.stdout` | z3 stdout |
| `config_sN.stderr` | z3 stderr (includes `-v:2` verbose stats) |
| `config_sN.z3log` | AST trace log (requires `--z3-log`) |

**Key options:**
- `--z3 PATH` — z3 binary (default: `~/ag/z3/z3-edge/build/z3`)
- `--verbosity N` — z3 `-v:N` flag (default: 2, 0 to disable)
- `--z3-log` — enable AST trace log (`trace=true`), requires `--save`
- `--format plain|json|rich` — output format (auto-detects TTY)

### 🔍 lemur stats — Trace file analyzer

Parse `.z3-trace` files and display structured statistics with tag-specific
analysis for `nla_solver` and `nra`.

```bash
# Summary of a trace file
lemur stats .z3-trace

# Filter to specific tag
lemur stats .z3-trace --tag nla_solver

# List all lemmas, one per line
lemur stats .z3-trace --lemma-list

# Show detailed variable table for lemma #3
lemur stats .z3-trace --lemma-detail 3

# Show details for lemmas 1 through 5
lemur stats .z3-trace --lemma-details 1:5

# Machine-readable output
lemur stats .z3-trace -f json

# Ignore varmap, show raw j-variables
lemur stats .z3-trace --lemma-detail 3 --no-varmap
```

**Lemma analysis** (for `~lemma_builder` entries in `nla_solver` tag):
- Strategy distribution with short names (pseudo-lin, grob-q, ord-binom, div-mono, etc.)
- `--lemma-list` — all lemmas at a glance, one row per lemma
- `--lemma-detail N` / `--lemma-details 1:5` — full variable tables
- Variable tables: value, bounds, definition, root, basic flag, SMT name (via varmap)
- Monomial highlighting (cyan), root mismatch detection (red)
- Variable delta tracking across consecutive lemmas (bounds tightening, value changes)
- Large constants humanized in Rich mode: `16384` → `2^14`, `16383` → `2^14-1`,
  numbers >= 1M digit-grouped: `1_062_993_921`. Power-of-2 forms colored bright_cyan.
- Varmap support: maps internal LP variables (j25) to SMT expressions (R21).
  Pre-humanized so `(mod R2 2^64)` instead of truncated raw numbers.

## 🐾 Z3 Trace Format

Z3 debug builds support tracing via `-tr:TAG`. Output goes to `.z3-trace`
in the working directory. Each entry:

```
-------- [TAG] function_name /path/to/file.cpp:LINE ---------
<free-form body>
------------------------------------------------
```

Relevant tags: `nla_solver` (and variants like `nla_solver_details`), `nra`,
`nlsat_*`. Tags are defined in `src/util/trace_tags.def`.

Z3 also has a second trace mechanism (`trace=true`, output to `z3.log`) that
logs AST construction events. Captured with `--z3-log` in lemur sweep. This is
all-or-nothing with no per-event filtering.

## 🌿 Architecture

```
pyproject.toml              Project config, deps, entry points
lemur/
  cli/
    main.py                 lemur entry point (subcommand dispatch)
    sweep.py                lemur sweep subcommand
    stats.py                lemur stats subcommand
  parsers.py                Trace block parser (header/body/footer) + varmap
  sweep.py                  Sweep engine (subprocess pool, temp dirs)
  table.py                  Rich/plain/JSON output formatting
  stats.py                  Per-tag statistics computation
  lemma.py                  ~lemma_builder structured parser
  report.py                 Lemma rendering, humanization, strategy names
tests/sample_traces/        Sample trace files for testing
```

## 👥 Contributors

- **Arie Gurfinkel** &lt;arie@certora.com&gt; — author and maintainer

Much of the implementation was iterated on with Claude Code (Opus 4.6 /
4.7). See `Co-Authored-By` trailers on individual commits for provenance.

Contributions welcome — open an issue or PR.

## 📜 License

MIT — see [`LICENSE`](LICENSE).

