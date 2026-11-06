import hashlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path

from lemur.sweep import RunConfig, run_single, run_sweep


def fake_solver(tmp_path):
    solver = tmp_path / "fake z3"
    solver.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "if '-version' in sys.argv:\n"
        "    print('Z3 version fake-test')\n"
        "else:\n"
        "    assert pathlib.Path(sys.argv[-1]).read_text().endswith('(check-sat)\\n')\n"
        "    pathlib.Path('.z3-trace').write_text('trace evidence\\n')\n"
        "    print('unsat')\n"
    )
    solver.chmod(0o755)
    return str(solver)


def test_sweep_saves_injected_inputs_and_portable_replay(tmp_path):
    solver = fake_solver(tmp_path)
    source = tmp_path / "original formula.smt2"
    source.write_text("(set-logic QF_BV)\n(check-sat)\n")
    saved = tmp_path / "saved runs"
    _, results = run_sweep(
        solver,
        str(source),
        [0, 1],
        [
            RunConfig("cfg.one", {"tactic.default_tactic": "(then simplify smt)"}),
            RunConfig("cfg two", {}),
        ],
        1,
        save_dir=str(saved),
        show_progress=False,
        splits=[("split.one", "(assert true)"), ("split.two", "(assert false)")],
        trace_tags=["bv"],
        stats=True,
    )
    assert len(results) == 8
    assert len(list((saved / "inputs").glob("*.smt2"))) == 2
    assert len(list(saved.glob("*.run.json"))) == 8
    for result in results:
        prefix = f"{result.split}.{result.config}_s{result.seed}"
        meta = json.loads((saved / f"{prefix}.run.json").read_text())
        data = (saved / meta["input"]["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == meta["input"]["sha256"]
        assert b"(assert " in data
        assert meta["returncode"] == 0
        assert meta["timed_out_by_runner"] is False
        assert meta["solver"]["version"] == "Z3 version fake-test"
        assert meta["status"] == "unsat"
        assert not Path(meta["invocation"]["cwd"]).exists()
        assert Path(shlex.split(result.cmdline)[-1]).read_bytes() == data
        assert meta["invocation"]["argv"] == shlex.split(result.cmdline)
        assert "-tr:bv" in meta["invocation"]["argv"]
        assert f"smt.random_seed={result.seed}" in meta["invocation"]["argv"]
        assert (saved / meta["artifacts"]["stderr"]).read_bytes() == b""
    moved = tmp_path / "relocated results"
    shutil.move(saved, moved)
    meta = json.loads(next(moved.glob("*.run.json")).read_text())
    replay = subprocess.run(
        meta["replay"]["argv"], cwd=moved, capture_output=True, text=True
    )
    assert replay.returncode == 0
    assert replay.stdout == "unsat\n"


def test_watchdog_timeout_keeps_evidence(tmp_path, monkeypatch):
    from lemur import sweep

    source = tmp_path / "query.smt2"
    source.write_text("(check-sat)\n")
    saved = tmp_path / "save"

    class Process:
        returncode = None
        calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("z3", timeout)
            return "", "partial diagnostic\n"

    process = Process()
    monkeypatch.setattr(sweep, "solver_identity", lambda _: {"version": "fake"})
    monkeypatch.setattr(sweep.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(
        sweep, "_kill_process_group", lambda p: setattr(p, "returncode", -15)
    )
    result = run_single(
        "z3",
        str(source),
        3,
        RunConfig("cfg.dot", {}),
        1,
        save_dir=str(saved),
        split_name="split.dot",
    )
    meta = json.loads((saved / "split.dot.cfg.dot_s3.run.json").read_text())
    assert result.split == "split.dot"
    assert result.status == "timeout"
    assert meta["timed_out_by_runner"] is True
    assert meta["returncode"] == -15
    assert (saved / meta["artifacts"]["stdout"]).read_bytes() == b""
    assert (saved / meta["artifacts"]["stderr"]).read_text() == "partial diagnostic\n"
    assert (saved / meta["input"]["path"]).read_bytes() == source.read_bytes()


def test_failed_launch_records_error(tmp_path):
    source = tmp_path / "query.smt2"
    source.write_text("(check-sat)\n")
    saved = tmp_path / "save"
    result = run_single(
        str(tmp_path / "missing"),
        str(source),
        0,
        RunConfig("default", {}),
        1,
        save_dir=str(saved),
    )
    meta = json.loads((saved / "default_s0.run.json").read_text())
    assert result.status == "error"
    assert meta["returncode"] is None
    assert meta["error"]
    assert meta["solver"]["version_error"]
    assert (saved / meta["artifacts"]["stdout"]).read_text() == ""
    assert (saved / meta["artifacts"]["stderr"]).read_text() == ""


def test_parallel_runs_share_one_input(tmp_path):
    solver = fake_solver(tmp_path)
    source = tmp_path / "query.smt2"
    source.write_text("(check-sat)\n")
    saved = tmp_path / "save"
    _, results = run_sweep(
        solver,
        str(source),
        [1, 2, 3, 4],
        [RunConfig("parallel", {})],
        1,
        jobs=2,
        save_dir=str(saved),
        show_progress=False,
    )
    assert len(results) == 4
    assert all(r.status == "unsat" for r in results)
    assert len(list((saved / "inputs").iterdir())) == 1
    assert len(list(saved.glob("*.run.json"))) == 4


def test_solver_timeout_is_distinct_from_watchdog(tmp_path):
    solver = tmp_path / "timeout-z3"
    solver.write_text(
        '#!/bin/sh\nif [ "$1" = "-version" ]; then echo fake; else echo timeout; fi\n'
    )
    solver.chmod(0o755)
    source = tmp_path / "query.smt2"
    source.write_text("(check-sat)\n")
    saved = tmp_path / "save"
    result = run_single(
        str(solver),
        str(source),
        0,
        RunConfig("solver-timeout", {}),
        1,
        save_dir=str(saved),
    )
    meta = json.loads((saved / "solver-timeout_s0.run.json").read_text())
    assert result.status == "timeout"
    assert meta["returncode"] == 0
    assert meta["timed_out_by_runner"] is False


def test_unsaved_generated_input_does_not_advertise_broken_replay(tmp_path):
    solver = fake_solver(tmp_path)
    source = tmp_path / "query.smt2"
    source.write_text("(check-sat)\n")
    _, results = run_sweep(
        solver,
        str(source),
        [0],
        [RunConfig("default", {})],
        1,
        show_progress=False,
        splits=[("injected", "(assert true)")],
    )
    assert results[0].status == "unsat"
    assert results[0].cmdline == ""
