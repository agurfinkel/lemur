"""
Sweep engine: run Z3 across seeds and configurations, collect results.

Each run gets its own temp directory (for trace file isolation).
Temp directories are cleaned up on completion and on unexpected exit.
"""

import atexit
import enum
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import CancelledError, ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from lemur.smt_inject import make_split_smt
from lemur.sweep_artifacts import preserve_input, solver_identity
from lemur.table import SweepTable, make_console
from lemur.z3_stats import parse_z3_stats  # noqa: F401  (re-export)


@dataclass
class RunConfig:
    """A named Z3 configuration (set of key=value params)."""

    name: str
    params: dict[str, str]

    @classmethod
    def parse(cls, spec: str) -> "RunConfig":
        """Parse 'name: key=val key=val ...' or '"name with spaces": key=val ...'.

        Values containing whitespace must be quoted, e.g.
          myconf: tactic.default_tactic="(then simplify smt)"
        """
        spec = spec.strip()
        # Handle quoted name
        if spec.startswith('"'):
            end_quote = spec.index('"', 1)
            name = spec[1:end_quote]
            rest = spec[end_quote + 1 :].lstrip(": ")
        else:
            parts = spec.split(":", 1)
            name = parts[0].strip()
            rest = parts[1].strip() if len(parts) > 1 else ""

        params = {}
        # shlex.split respects quoting so whitespace inside quoted values is
        # preserved (e.g. tactic.default_tactic="(then simplify smt)").
        for token in shlex.split(rest):
            if "=" in token:
                k, v = token.split("=", 1)
                params[k] = v
        return cls(name=name, params=params)


@dataclass
class RunResult:
    config: str
    seed: int
    status: str  # sat, unsat, unknown, timeout, error
    time_s: float
    stdout: str
    stderr: str
    trace_file: Path | None  # path to saved trace, if any
    cmdline: str = ""  # copy-pasteable z3 command for manual re-run
    stats: dict | None = None  # parsed z3 -st stats, when --stats enabled
    split: str | None = None  # split name, when --split used


class StopAction(enum.Enum):
    """Disposition returned by process_result: what the sweep should do next."""

    NONE = "none"  # record & continue
    GLOBAL = "global"  # abort the whole sweep
    PER_SPLIT = "per_split"  # prune just this split's remaining work


# Temp dirs created by in-flight runs, cleaned up on normal or abnormal exit.
_active_temp_dirs: set[str] = set()


def _cleanup_temp_dirs():
    for d in list(_active_temp_dirs):
        shutil.rmtree(d, ignore_errors=True)
    _active_temp_dirs.clear()


atexit.register(_cleanup_temp_dirs)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Best-effort termination of a subprocess's whole process group."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def run_single(
    z3_bin: str,
    smt_file: str,
    seed: int,
    config: RunConfig,
    timeout: int,
    trace_tags: list[str] | None = None,
    verbosity: int = 2,
    z3_log: bool = False,
    save_dir: str | None = None,
    parent_tmpdir: str | None = None,
    stats: bool = False,
    split_name: str | None = None,
) -> RunResult:
    """Run a single Z3 invocation in a temp directory."""

    name_tag = f"{split_name}.{config.name}" if split_name else config.name
    tmpdir = tempfile.mkdtemp(prefix=f"lemur_{name_tag}_s{seed}_", dir=parent_tmpdir)
    _active_temp_dirs.add(tmpdir)

    cmdline = ""
    proc: subprocess.Popen | None = None
    trace_dest = None
    stdout, stderr = "", ""
    timed_out = False
    error = None
    start = time.monotonic()
    cmd = []
    input_info = None
    solver_info = None
    try:
        # Solve the saved snapshot so its digest identifies the actual input.
        if save_dir:
            save_root = Path(save_dir).resolve()
            input_info = preserve_input(Path(smt_file), save_root)
            smt_file = str(save_root / input_info["path"])
            solver_info = solver_identity(z3_bin)
        cmd = [z3_bin]
        if trace_tags:
            cmd.extend(f"-tr:{tag}" for tag in trace_tags)
        cmd.extend(f"{k}={v}" for k, v in config.params.items())
        if z3_log:
            cmd.extend(["trace=true", "trace_file_name=z3.log"])
        if stats:
            cmd.append("-st")
        cmd.extend(
            [
                f"-v:{verbosity}",
                f"sat.random_seed={seed}",
                f"smt.random_seed={seed}",
                f"nlsat.seed={seed}",
                f"-T:{timeout}",
                str(Path(smt_file).resolve()),
            ]
        )
        # A relative executable path must survive the isolated solver cwd.
        if os.path.dirname(z3_bin):
            cmd[0] = str(Path(z3_bin).resolve())
        cmdline = shlex.join(cmd)
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=tmpdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout + 10)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            _kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stdout, stderr = exc.output or "", exc.stderr or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode(errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors="replace")
    except (KeyboardInterrupt, SystemExit):
        if proc is not None:
            _kill_process_group(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        _active_temp_dirs.discard(tmpdir)
        raise
    except Exception as exc:
        if proc is not None:
            _kill_process_group(proc)
        error = str(exc)

    elapsed = time.monotonic() - start
    first_line = stdout.strip().split("\n")[0].strip()
    status = (
        first_line if first_line in {"sat", "unsat", "unknown", "timeout"} else "error"
    )
    if timed_out:
        status = "timeout"
    elif error:
        status = "error"
    stats_data = parse_z3_stats(stdout) if stats else None

    try:
        if save_dir:
            save_root = Path(save_dir).resolve()
            save_root.mkdir(parents=True, exist_ok=True)
            # Append suffixes to preserve dotted configuration and split names.
            stem = f"{name_tag}_s{seed}"
            prefix = save_root / stem
            artifacts = {}
            for suffix, contents in (("stdout", stdout), ("stderr", stderr)):
                dest = Path(str(prefix) + "." + suffix)
                dest.write_text(contents)
                artifacts[suffix] = dest.name
            for source, suffix in ((".z3-trace", "trace"), ("z3.log", "z3log")):
                src = Path(tmpdir) / source
                if src.exists():
                    dest = Path(str(prefix) + "." + suffix)
                    shutil.copy2(src, dest)
                    artifacts[suffix] = dest.name
                    if suffix == "trace":
                        trace_dest = dest
            if stats_data is not None:
                dest = Path(str(prefix) + ".stats.json")
                dest.write_text(json.dumps(stats_data, indent=2) + "\n")
                artifacts["stats"] = dest.name
            replay_argv = cmd.copy()
            if input_info and replay_argv:
                replay_argv[-1] = input_info["path"]
            manifest = {
                "schema_version": 1,
                "config": config.name,
                "seed": seed,
                "split": split_name,
                "status": status,
                "time_s": elapsed,
                "returncode": proc.returncode if proc else None,
                "timed_out_by_runner": timed_out,
                "error": error,
                "input": input_info,
                "solver": solver_info,
                "invocation": {"argv": cmd, "cwd": tmpdir},
                "replay": {"argv": replay_argv, "cwd": "."},
                "artifacts": artifacts,
            }
            Path(str(prefix) + ".run.json").write_text(
                json.dumps(manifest, indent=2) + "\n"
            )
        return RunResult(
            config=config.name,
            seed=seed,
            status=status,
            time_s=elapsed,
            stdout=stdout,
            stderr=stderr if not error else stderr + error,
            trace_file=trace_dest,
            cmdline=cmdline,
            stats=stats_data,
            split=split_name,
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _active_temp_dirs.discard(tmpdir)


def _init_worker():
    """Ensure SIGINT raises KeyboardInterrupt in pool workers so run_single's
    cleanup path runs and the z3 subprocess is killed before the worker dies."""
    signal.signal(signal.SIGINT, signal.default_int_handler)


def _shutdown_pool_workers(executor: ProcessPoolExecutor) -> None:
    """Give workers a moment to self-clean on SIGINT, then force any stragglers.

    Each worker's run_single kills its z3 child and removes its tmpdir when
    KeyboardInterrupt fires. We send SIGINT explicitly (not relying on the
    shell's group signal) and wait briefly before SIGTERM/SIGKILL."""
    procs = list(getattr(executor, "_processes", {}).values())
    for p in procs:
        try:
            p.send_signal(signal.SIGINT)
        except Exception:
            pass
    deadline = time.monotonic() + 3.0
    for p in procs:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            p.join(timeout=remaining)
        except Exception:
            pass
    for p in procs:
        try:
            if p.is_alive():
                p.terminate()
        except Exception:
            pass


def _cancel_and_shutdown(executor, futures):
    for fut in futures:
        fut.cancel()
    _shutdown_pool_workers(executor)
    executor.shutdown(wait=False, cancel_futures=True)


def _run_parallel(
    work,
    jobs,
    process_result,
    closed_splits,
    *,
    z3_bin,
    timeout,
    trace_tags,
    verbosity,
    z3_log,
    save_dir,
    parent_tmpdir,
    stats,
):
    """Run work items in a process pool. Each work item is
    (split_name, smt_path, config, seed). `process_result(result)` returns
    a StopAction indicating how to proceed. `closed_splits` is mutated by
    process_result when a split closes; this function also inspects it to
    decide when to prune pending work or abort early.

    Trade-off: futures are submitted up-front, so a closed split's
    already-pending work pays its pickling cost before being cancelled.
    With -j typically 4-16, the waste is bounded by `jobs - 1` z3
    processes draining naturally after closure."""
    executor = ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker)
    futures = {}
    stopped_early = False
    try:
        for split_name, smt_path, config, seed in work:
            f = executor.submit(
                run_single,
                z3_bin,
                smt_path,
                seed,
                config,
                timeout,
                trace_tags,
                verbosity,
                z3_log,
                save_dir,
                parent_tmpdir,
                stats,
                split_name,
            )
            futures[f] = (split_name, config.name, seed)

        for f in as_completed(futures):
            try:
                result = f.result()
            except CancelledError:
                # Pending future belonging to a split we already closed.
                # No RunResult to record; skip silently.
                continue
            except BrokenProcessPool:
                # A worker died unexpectedly; treat as interruption.
                raise KeyboardInterrupt from None
            action = process_result(result)
            if action is StopAction.GLOBAL:
                stopped_early = True
                break
            if action is StopAction.PER_SPLIT:
                # Cancel pending futures for the just-closed split. Running
                # ones finish naturally; their results stream and tally but
                # don't re-trigger closure (guard in process_result).
                split_name = result.split
                for fut, (fsname, _c, _s) in list(futures.items()):
                    if fsname == split_name and not fut.done():
                        fut.cancel()
                # Fast path: every remaining future belongs to a closed
                # split — kill everything now (no collateral damage since
                # nothing else is waiting on these).
                remaining = [ids for fut, ids in futures.items() if not fut.done()]
                if remaining and all(sn in closed_splits for sn, _, _ in remaining):
                    stopped_early = True
                    break
    except KeyboardInterrupt:
        _cancel_and_shutdown(executor, futures)
        raise
    else:
        if stopped_early:
            _cancel_and_shutdown(executor, futures)
        else:
            executor.shutdown(wait=True)


def run_sweep(
    z3_bin: str,
    smt_file: str,
    seeds: list[int],
    configs: list[RunConfig],
    timeout: int,
    jobs: int = 1,
    trace_tags: list[str] | None = None,
    verbosity: int = 2,
    z3_log: bool = False,
    save_dir: str | None = None,
    show_progress: bool = True,
    on_result=None,
    stop_when=None,
    stats: bool = False,
    splits: list[tuple[str, str]] | None = None,
    stop_per_split_when=None,
    leaf_files: list[tuple[str, str]] | None = None,
    pre_closed_splits: dict[str, str] | None = None,
) -> tuple[SweepTable, list[RunResult]]:
    """Run a full sweep and return a populated SweepTable and all RunResults.

    If `on_result` is given, it is invoked with each RunResult as soon as
    that run finishes (enables streaming CSV output from the CLI).

    If `stop_when(result)` returns truthy, the sweep aborts after processing
    that result: pending futures are cancelled and running workers killed.

    If `stop_per_split_when(result)` returns truthy AND result.split is set,
    that split is marked closed: its pending (not-yet-running) work is
    cancelled, while other splits keep running. First-match semantics —
    subsequent matching results for an already-closed split are recorded
    and streamed but have no control-flow effect.

    If `splits` is given (list of (name, smt_to_inject)), a modified benchmark
    is generated per split and the sweep cross-products splits × configs × seeds.

    If `leaf_files` is given (list of (split_name, smt_path)), each entry is
    an already-complete leaf SMT file. No injection is applied; the file is
    used as-is. Mutually exclusive with `splits`.

    If `pre_closed_splits` is given (dict of split_name → reason), synthetic
    UNSAT results with time_s=0 are recorded for those splits at sweep start
    so the tally's per-split closure summary marks them closed without
    spawning z3 (used for plan.json's `pruned` leaves).
    """
    if splits and leaf_files:
        raise ValueError("run_sweep: `splits` and `leaf_files` are mutually exclusive")

    if save_dir:
        os.makedirs(save_dir, exist_ok=True)

    config_names = [c.name for c in configs]
    table = SweepTable(config_names, seeds)
    all_results: list[RunResult] = []
    status_counts = {"sat": 0, "unsat": 0, "timeout": 0, "unknown": 0, "error": 0}
    closed_splits: set[str] = set()
    # Count total splits (for the `closed=N/M` progress readout): prefer the
    # explicit `splits` / `leaf_files` list, then fall back to pre-closed.
    if splits:
        n_splits_total = len(splits)
    elif leaf_files:
        n_splits_total = len(leaf_files) + (
            len(pre_closed_splits) if pre_closed_splits else 0
        )
    else:
        n_splits_total = 0

    # Per-sweep parent tmpdir: every run_single tmpdir nests under this, so a
    # single rmtree at the end cleans up even if a worker was killed mid-cleanup.
    sweep_tmp = tempfile.mkdtemp(prefix="lemur_sweep_")
    _active_temp_dirs.add(sweep_tmp)

    # Build (split_name, smt_path) tuples. None split = original benchmark.
    if splits:
        split_files: list[tuple[str | None, str]] = []
        for name, inject in splits:
            dest = os.path.join(sweep_tmp, f"split_{name}.smt2")
            make_split_smt(smt_file, inject, dest)
            split_files.append((name, dest))
    elif leaf_files:
        split_files = [(name, path) for name, path in leaf_files]
    else:
        split_files = [(None, smt_file)]

    # Build work items: split × config × seed
    work = [
        (sname, spath, config, seed)
        for sname, spath in split_files
        for config in configs
        for seed in seeds
    ]
    # Pre-closed splits emit exactly one synthetic result each, so they
    # count toward the progress-bar total.
    total = len(work) + (len(pre_closed_splits) if pre_closed_splits else 0)

    if show_progress:
        console = make_console()
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        )
        task_id = progress.add_task(
            "Sweeping  sat=0 unsat=0 to=0 unk=0 err=0", total=total
        )
    else:
        progress = None

    def _tally_desc() -> str:
        base = (
            f"Sweeping  "
            f"[green]sat={status_counts['sat']}[/green] "
            f"[cyan]unsat={status_counts['unsat']}[/cyan] "
            f"[red]to={status_counts['timeout']}[/red] "
            f"[yellow]unk={status_counts['unknown']}[/yellow] "
            f"err={status_counts['error']}"
        )
        if stop_per_split_when is not None and n_splits_total:
            base += f"  [magenta]closed={len(closed_splits)}/{n_splits_total}[/magenta]"
        return base

    def process_result(result: RunResult) -> StopAction:
        """Record the result and return the appropriate StopAction."""
        if splits and not save_dir:
            # Injected inputs are temporary; replay requires --save.
            result.cmdline = ""
        table.add_result(result.config, result.seed, result.status, result.time_s)
        all_results.append(result)
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
        if on_result is not None:
            on_result(result)
        if progress:
            progress.update(task_id, advance=1, description=_tally_desc())

        if stop_when is not None and stop_when(result):
            return StopAction.GLOBAL
        if (
            stop_per_split_when is not None
            and result.split is not None
            and result.split not in closed_splits
            and stop_per_split_when(result)
        ):
            closed_splits.add(result.split)
            return StopAction.PER_SPLIT
        return StopAction.NONE

    # Emit synthetic UNSAT results for pre-closed splits (e.g., plan.json's
    # pruned leaves). They get streamed, tallied, and count toward per-split
    # closure without spawning z3.
    if pre_closed_splits:
        # Use the first configured config as the "closer" label; seed 0.
        closer_config = configs[0].name if configs else "default"
        for sname, reason in pre_closed_splits.items():
            synthetic = RunResult(
                config=closer_config,
                seed=0,
                status="unsat",
                time_s=0.0,
                stdout="",
                stderr=f"[lemur] pre-closed: {reason}",
                trace_file=None,
                cmdline="",
                stats=None,
                split=sname,
            )
            # Direct fan-out: record + stream + notify stop predicates.
            # stop actions from pre-closed results are honored (they close
            # the split for per-split semantics).
            action = process_result(synthetic)
            # GLOBAL stop from a synthetic unsat would be surprising; we
            # ignore it and continue so the actual sweep still runs.
            # (User who wants global-abort on first unsat should not be
            #  using pre-closed splits in the first place.)
            del action

    ctx = progress if progress else _nullcontext()
    try:
        with ctx:
            if jobs == 1:
                for sname, spath, config, seed in work:
                    if sname in closed_splits:
                        continue
                    result = run_single(
                        z3_bin,
                        spath,
                        seed,
                        config,
                        timeout,
                        trace_tags,
                        verbosity,
                        z3_log,
                        save_dir,
                        parent_tmpdir=sweep_tmp,
                        stats=stats,
                        split_name=sname,
                    )
                    action = process_result(result)
                    if action is StopAction.GLOBAL:
                        break
                    # PER_SPLIT and NONE both continue; the closed_splits
                    # set grows and the skip at the top prunes remaining
                    # work for closed splits.
            else:
                _run_parallel(
                    work,
                    jobs,
                    process_result,
                    closed_splits,
                    z3_bin=z3_bin,
                    timeout=timeout,
                    trace_tags=trace_tags,
                    verbosity=verbosity,
                    z3_log=z3_log,
                    save_dir=save_dir,
                    parent_tmpdir=sweep_tmp,
                    stats=stats,
                )
    except KeyboardInterrupt:
        print(
            "\n[interrupted] killing running z3 processes and cleaning up...",
            file=sys.stderr,
        )
        _cleanup_temp_dirs()
        sys.exit(130)
    finally:
        shutil.rmtree(sweep_tmp, ignore_errors=True)
        _active_temp_dirs.discard(sweep_tmp)

    return table, all_results


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def parse_seed_range(spec: str) -> list[int]:
    """Parse seed specification: '0-15', '1,3,5', '0-3,7,10-12'."""
    seeds = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            seeds.extend(range(int(lo), int(hi) + 1))
        else:
            seeds.append(int(part))
    return seeds
