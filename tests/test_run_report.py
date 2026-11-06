import json
import subprocess
import sys
from pathlib import Path

import pytest

from lemur.run_report import build_report, parse_verbose, progress, render_plain

FIXTURES = Path(__file__).parent / "sample_runs"


def report(name):
    return build_report(FIXTURES / f"{name}.stdout", FIXTURES / f"{name}.stderr")


def test_timeout_sat_search_survives_missing_final_counters():
    r = report("timeout_sat")
    assert r["verdict"] == "timeout"
    assert not any(k.startswith("sat-") for k in r["statistics"])
    assert r["engines"]["sat"]["search_observed"]
    last = r["engines"]["sat"]["last_progress"]
    assert last["fields"]["conflicts"] == 5675
    assert last["fields"]["search_elapsed_s"] == 14.11
    assert any("SAT counters are unavailable" in s for s in r["warnings"])
    circuit = next(x for x in r["verbose"] if x["kind"] == "circuit")
    assert circuit["complete"]
    assert circuit["fields"]["vars"] == 321076
    blast = next(x for x in r["verbose"] if x.get("name") == "bit-blast")
    assert blast["fields"]["num-exprs"] == 657301
    assert blast["fields"]["time"] == 1.05
    assert "SAT: search observed" in render_plain(r)
    assert json.loads(json.dumps(r)) == r


def test_smt_timeout_exposes_row_work_without_inventing_time():
    r = report("timeout_smt")
    assert r["statistics"]["array-ax2"] == 582830
    assert r["engines"]["smt"]["search_observed"]
    last = r["engines"]["smt"]["last_progress"]["fields"]
    assert last["conflicts"] == 748
    assert last["propagations"] == 4590507
    assert "search_elapsed_s" not in last
    assert "array-ax2" in render_plain(r)


def test_completed_sat_has_counters_and_no_missing_counter_warning():
    r = report("unsat_sat")
    assert r["verdict"] == "unsat"
    assert r["statistics"]["sat-conflicts"] == 11590
    assert not r["warnings"]


@pytest.mark.parametrize(
    "engine,body",
    [("sat", "1 2 3 4/5 6/7 8 9"), ("smt", "1 2 3 4 5/6/7 8/9 10 11")],
)
def test_platform_layout_without_memory_or_time(engine, body):
    p = progress(engine, body)
    assert p is not None
    assert "memory_mib" not in p
    assert "search_elapsed_s" not in p


def test_unknown_layout_and_headers_are_retained():
    text = "(sat.stats :conflicts :restarts)\n(sat.stats 1 2 3 4/5 6/7 8 9 extra)\nnew log shape"
    records = parse_verbose(text)
    assert [r["kind"] for r in records] == ["header", "unparsed", "unparsed"]
    assert records[1]["reason"] == "unknown_progress_layout"
    assert records[2]["raw"] == "new log shape"
    assert records[2]["line_start"] == 3


def test_partial_circuit_does_not_swallow_next_record():
    records = parse_verbose("(sat-status\n :vars 42\n(sat.solver)\n")
    assert records[0]["complete"] is False
    assert records[0]["fields"] == {"vars": 42}
    assert records[0]["line_end"] == 2
    assert records[1]["event"] == "search_start"


def test_empty_and_multiple_result_logs(tmp_path):
    r = build_report()
    assert r["verdict"] is None and r["statistics"] is None
    assert r["engines"] == {}
    out = tmp_path / "run.stdout"
    out.write_text("sat\nunsat\ntimeout\n")
    r = build_report(out)
    assert r["verdict"] == "timeout"
    assert len(r["results"]) == 3
    assert any("Multiple result" in s for s in r["warnings"])


def test_timers_and_counter_resets_remain_separate():
    rows = parse_verbose(
        "(outer :time 2.00 :num-exprs 30)\n"
        "(inner :time 1.00 :num-exprs 20)\n"
        "(sat.stats 10 20 0 30/10 2/1 1 0 10.0 2.0)\n"
        "(sat.stats 0 0 0 30/10 0/0 1 0 9.0 0.0)\n"
    )
    assert len(rows) == 4
    assert rows[-1]["fields"]["search_elapsed_s"] == 0.0
    assert rows[0]["fields"]["time"] == 2.0


def test_optional_trace_reports_unknown_tags_and_truncation(tmp_path):
    p = tmp_path / "run.trace"
    p.write_bytes(b"unframed\n-------- [novel] f source.cpp:3 ---------\npartial")
    r = build_report(trace=p)
    assert r["trace"]["records"] == {"raw": 1, "trace": 1}
    assert r["trace"]["sites"][0]["tag"] == "novel"
    assert r["trace"]["incomplete"][0]["reason"] == "eof"
    assert any("incomplete blocks" in s for s in r["warnings"])


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "lemur.cli.main", "run-report", *args],
        capture_output=True,
        text=True,
    )


def test_cli_prefix_json_and_explicit_paths():
    p = cli(str(FIXTURES / "timeout_sat"), "-f", "json")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["verdict"] == "timeout"
    p = cli("--stderr", str(FIXTURES / "timeout_smt.stderr"))
    assert p.returncode == 0
    assert "Result: not recorded" in p.stdout
    assert "SMT: search observed" in p.stdout


def test_cli_errors_and_agent_help():
    assert cli().returncode == 2
    assert cli("--stdout", "missing.stdout").returncode == 1
    p = cli("--agent")
    assert p.returncode == 0
    assert "search-local" in p.stdout


def test_multiple_stats_blocks_do_not_hide_missing_sat_counters(tmp_path):
    from lemur.z3_stats import parse_z3_run

    out = tmp_path / "run.stdout"
    out.write_text("sat\n(:sat-decisions 10)\ntimeout\n(:rlimit-count 20)\n")
    err = tmp_path / "run.stderr"
    err.write_text("(sat.solver)\n")
    r = build_report(out, err)
    assert r["statistics"] == {"rlimit-count": 20}
    assert any("SAT counters are unavailable" in s for s in r["warnings"])
    assert parse_z3_run(out.read_text()) == ("timeout", {"rlimit-count": 20})


def test_truncated_final_stats_does_not_reuse_old_block(tmp_path):
    out = tmp_path / "run.stdout"
    out.write_text("sat\n(:sat-decisions 10)\ntimeout\n(:rlimit-count 20\n")
    assert build_report(out)["statistics"] is None
