from pathlib import Path

import pytest

from lemur.run_report import (
    build_report,
    diagnose_termination,
    render_plain,
    summarize_report,
)


def test_actual_memory_failure_is_not_an_unknown_termination():
    path = Path(__file__).parent / "sample_runs" / "out_of_memory.stderr"
    report = build_report(stderr=path)
    assert report["verdict"] is None
    term = report["termination"]
    assert term["cause"] == "out_of_memory"
    assert term["evidence"][-1]["text"] == '(error "out of memory")'
    assert term["evidence"][-1]["line"] == len(path.read_text().splitlines())
    assert summarize_report(report)["termination"] == term
    assert "Termination: out of memory" in render_plain(report)
    assert not any("termination reason is unknown" in w for w in report["warnings"])


@pytest.mark.parametrize(
    "text", ['(error "out of memory")', "ERROR: out of memory", "out of memory"]
)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_explicit_memory_diagnostics(text, stream):
    inputs = {"stdout": "", "stderr": "", "verdict": None}
    inputs[stream] = text
    result = diagnose_termination(**inputs)
    assert result["cause"] == "out_of_memory"
    assert result["evidence"][0]["stream"] == stream


def test_result_and_later_failure_remain_distinct(tmp_path):
    out = tmp_path / "run.stdout"
    err = tmp_path / "run.stderr"
    out.write_text("sat\n")
    err.write_text('(error "out of memory")\n')
    r = build_report(out, err)
    assert r["verdict"] == "sat"
    assert r["termination"]["cause"] == "out_of_memory"


def test_conflicting_diagnostics_are_not_ordered_across_streams():
    r = diagnose_termination("timeout\n", '(error "out of memory")', "timeout")
    assert r["cause"] == "multiple"
    assert r["reported_causes"] == ["out_of_memory", "timeout"]
    assert len(r["evidence"]) == 2


def test_unknown_error_is_not_guessed_as_resource_limit():
    r = diagnose_termination('(error "unsupported operator")', "", None)
    assert r["cause"] == "solver_error"
    assert r["evidence"][0]["text"] == '(error "unsupported operator")'


def test_memory_counter_or_comment_does_not_imply_oom():
    r = diagnose_termination("", "; out of memory\n(memory :max-memory 999)\n", None)
    assert r == {"cause": "unknown", "reported_causes": [], "evidence": []}


def test_result_alone_does_not_claim_clean_process_exit():
    assert diagnose_termination("unsat\n", "", "unsat")["cause"] == "result_reported"
    assert diagnose_termination("timeout\n", "", "timeout")["cause"] == "timeout"
    assert (
        diagnose_termination("", "timeout (process killed)\n", None)["cause"]
        == "timeout"
    )


def test_manifest_exit_metadata_and_limit_context(tmp_path):
    import json

    meta = tmp_path / "run.run.json"
    meta.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "returncode": -9,
                "timed_out_by_runner": False,
                "invocation": {"argv": ["z3", "memory_max_size=2048", "-T:30"]},
            }
        )
    )
    r = build_report(run_metadata=meta)
    assert r["termination"]["cause"] == "signal"
    assert r["termination"]["returncode"] == -9
    assert r["termination"]["configured_limits"] == {
        "memory_max_size": "2048",
        "timeout_s": "30",
    }
    assert "terminated by signal" in render_plain(r)
    data = json.loads(meta.read_text())
    data["timed_out_by_runner"] = True
    meta.write_text(json.dumps(data))
    assert build_report(run_metadata=meta)["termination"]["cause"] == "timeout"


def test_invalid_run_metadata_is_not_silently_trusted(tmp_path):
    meta = tmp_path / "run.json"
    meta.write_text('{"schema_version": 99}')
    with pytest.raises(ValueError, match="unsupported run metadata"):
        build_report(run_metadata=meta)
