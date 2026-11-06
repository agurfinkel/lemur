# Lemur development notes

## research notes

Research / investigation notes at: `/Users/arie/ag/lemur-research/`
(governed by the `better-than-fish` skill — see its `SKILL.md` for
conventions, and `~/ag/lemur-research/CLAUDE.md` for project-specific
triggers and code anchors).

## setup

```bash
uv sync --locked
```

This creates `.venv`, installs the development dependencies, and installs `lemur`
as a command with subcommands `sweep` and `stats`. Run commands with `uv run`,
for example `uv run lemur --help` and `uv run pytest`.

## repo layout

```
pyproject.toml              project config, deps, entry points
lemur/
  __init__.py
  cli/
    __init__.py
    main.py                 lemur entry point (subcommand dispatch)
    sweep.py                lemur sweep subcommand
    stats.py                lemur stats subcommand
  parsers.py                trace block parser + varmap
  sweep.py                  sweep engine (subprocess pool, temp dirs)
  table.py                  Rich/plain/JSON output formatting
  stats.py                  per-tag statistics computation
  lemma.py                  ~lemma_builder structured parser
  report.py                 lemma rendering, humanization, strategy names
tests/sample_traces/        sample trace files for testing
```

Single entry point `lemur` defined in `pyproject.toml`. Subcommands registered
via `register(subparsers)` pattern in `lemur/cli/sweep.py` and `lemur/cli/stats.py`.
To add a new subcommand: create `lemur/cli/foo.py` with `register()` and `run()`,
import and register in `main.py`.

## z3

Debug binary with tracing: `~/ag/z3/z3-edge/build/z3` (v4.17.0).
System z3 (`/opt/homebrew/bin/z3`) does NOT support tracing.
Benchmarks: `~/ag/z3/z3-edge/bench/`.

## trace format

TRACE macro output (`.z3-trace`):
```
-------- [TAG] function_name /path/file.cpp:LINE ---------
<body>
------------------------------------------------
```
STRACE has no header/footer (not parsed). Tags defined in `z3-edge/src/util/trace_tags.def`.

AST trace log (`z3.log`): line-oriented `[event-type] data`. Enabled by `trace=true`. All-or-nothing, no per-event filtering.

## conventions

- Use `uv sync --locked` and `uv run` for the project environment.
- After changing dependencies, run `uv lock` and commit `pyproject.toml` with `uv.lock`.
- Build distributions with `uv build`; optional integrations use `--extra split` / `--extra plot`.
- Output: Rich for TTY, plain when piped. All tools support `-f plain|json|rich`
- Parsers are modular by tag. Adding a new CTRACE tag = new analyzer function in `stats.py`
- Lemma analysis lives in `lemma.py` (parsing) and `report.py` (rendering)

## Before committing

Use Conventional Commits (`type(scope): subject`, scope optional), with a
header of at most 72 characters; see `.commitlintrc.yml`. CI checks each
commit in the PR. Use a `style:` commit for formatting-only changes.

Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
Use `uv run ruff format .` to format code. CI repeats these checks on PRs.
