"""Match identity must not depend on Z3's configurable pretty printer."""

import json
from types import SimpleNamespace

import pytest

from lemur import sgrep
from lemur.cli.sgrep import _do_run

z3 = pytest.importorskip("z3")


def multiplication_goal():
    x, y = z3.BitVecs("x y", 32)
    shared = x * 24
    other = y * 24
    goal = z3.Goal()
    goal.add(shared == 0, shared == other)
    return goal, sgrep.parse_pattern("(bvmul ?a ?b)")


@pytest.mark.parametrize("mode", ["list", "distinct"])
@pytest.mark.parametrize("output_format", ["json", "plain"])
def test_kind_output_never_prints_compound_expressions(
    mode, output_format, monkeypatch, capsys
):
    goal, pattern = multiplication_goal()

    def reject_expression_printing(self):
        raise AssertionError("kind output must not print expressions")

    monkeypatch.setattr(z3.ExprRef, "__str__", reject_expression_printing)
    args = SimpleNamespace(format=output_format, show="kind")
    _do_run(z3, sgrep, goal, pattern, mode, args)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    if output_format == "json":
        records = [json.loads(line) for line in lines]
        assert all(record["kind"] == "Op(bvmul)" for record in records)
        assert {record["captures"]["a"] for record in records} == {
            "Var(x)",
            "Var(y)",
        }
    else:
        assert all(line.startswith("Op(bvmul)") for line in lines)


def test_distinct_preserves_nodes_with_identical_printed_forms(monkeypatch, capsys):
    goal, pattern = multiplication_goal()
    monkeypatch.setattr(z3.ExprRef, "__str__", lambda self: "...")
    args = SimpleNamespace(format="json", show=None)
    _do_run(z3, sgrep, goal, pattern, "distinct", args)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records == [{"expr": "..."}, {"expr": "..."}]


def test_count_and_distinct_agree_on_shared_dag_nodes(capsys):
    goal, pattern = multiplication_goal()
    args = SimpleNamespace(format="json", show="kind")
    _do_run(z3, sgrep, goal, pattern, "count", args)
    assert json.loads(capsys.readouterr().out) == {"count": 2}
    _do_run(z3, sgrep, goal, pattern, "distinct", args)
    assert len(capsys.readouterr().out.splitlines()) == 2
