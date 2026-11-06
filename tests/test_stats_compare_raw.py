"""Tests for raw z3 -st input mode of lemur stats-compare."""

import json
from pathlib import Path

import pytest

from lemur.stats_compare import (
    load_stats_files,
    to_csv,
    to_json,
)
from lemur.z3_stats import parse_z3_run

SAMPLES = Path(__file__).parent / "sample_traces"
SAT_OUT = SAMPLES / "raw_z3_stats_sat.out"
UNSAT_OUT = SAMPLES / "raw_z3_stats_unsat.out"


def test_parse_z3_run_sat():
    text = SAT_OUT.read_text()
    result, stats = parse_z3_run(text)
    assert result == "sat"
    assert isinstance(stats, dict)
    assert stats["max-memory"] == pytest.approx(17.76)
    assert stats["rlimit-count"] == 189


def test_parse_z3_run_unsat():
    text = UNSAT_OUT.read_text()
    result, stats = parse_z3_run(text)
    assert result == "unsat"
    assert stats["conflicts"] == 1
    assert stats["num-checks"] == 1


def test_parse_z3_run_unknown_with_reason():
    text = "unknown\n(:reason-unknown timeout :max-memory 12.5 :time 0.50)\n"
    result, stats = parse_z3_run(text)
    assert result == "unknown"
    assert stats["reason-unknown"] == "timeout"
    assert stats["max-memory"] == pytest.approx(12.5)


def test_parse_z3_run_no_stats():
    result, stats = parse_z3_run("garbage\nmore garbage\n")
    assert result is None
    assert stats is None


def test_parse_z3_run_picks_last_result_token():
    text = "sat\n(check-sat)\nunsat\n(:foo 1 :bar 2)\n"
    result, stats = parse_z3_run(text)
    assert result == "unsat"
    assert stats == {"foo": 1, "bar": 2}


def test_load_stats_files_two_configs():
    cmp = load_stats_files([("A", str(SAT_OUT)), ("B", str(UNSAT_OUT))])
    assert cmp.configs == ["A", "B"]
    assert cmp.seed_counts == {"A": 1, "B": 1}
    assert cmp.results == {"A": ["sat"], "B": ["unsat"]}
    # max-memory present in both, with both readings stored.
    assert "A" in cmp.values["max-memory"]
    assert "B" in cmp.values["max-memory"]
    # conflicts only present in unsat config.
    assert "A" not in cmp.values.get("conflicts", {})
    assert "B" in cmp.values["conflicts"]


def test_load_stats_files_groups_seeds():
    # Same file under one label twice -> n=2 for that label.
    cmp = load_stats_files([("A", str(SAT_OUT)), ("A", str(SAT_OUT))])
    assert cmp.seed_counts == {"A": 2}
    assert cmp.results == {"A": ["sat", "sat"]}
    assert len(cmp.values["max-memory"]["A"]) == 2


def test_csv_includes_result_row():
    cmp = load_stats_files([("A", str(SAT_OUT)), ("B", str(UNSAT_OUT))])
    csv_out = to_csv(cmp)
    lines = csv_out.strip().splitlines()
    # Header + result row + N stat rows
    assert lines[0].startswith("stat,A,B")
    assert lines[1].startswith("result,sat,unsat")


def test_json_includes_results_when_present():
    cmp = load_stats_files([("A", str(SAT_OUT)), ("B", str(UNSAT_OUT))])
    obj = json.loads(to_json(cmp))
    assert obj["results"] == {"A": ["sat"], "B": ["unsat"]}
    assert obj["configs"] == ["A", "B"]


def test_load_stats_files_skips_unparseable(capsys, tmp_path):
    bogus = tmp_path / "bogus.out"
    bogus.write_text("hello world, no stats here\n")
    cmp = load_stats_files([("X", str(bogus)), ("A", str(SAT_OUT))])
    err = capsys.readouterr().err
    assert "no z3 -st content" in err
    assert cmp.configs == ["A"]


def test_summarize_results_mixed():
    from lemur.stats_compare import _summarize_results

    assert _summarize_results([]) == "—"
    assert _summarize_results(["sat"]) == "sat"
    assert _summarize_results(["sat", "sat"]) == "sat"
    s = _summarize_results(["sat", "unsat"])
    assert "sat(1)" in s and "unsat(1)" in s
