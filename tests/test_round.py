"""Tests for the streaming round-data parser used by `lemur n-over-time`."""

import json
import subprocess
import sys

from lemur.round import (
    RoundLemmaRange,
    lemma_ranges_per_round,
    parse_round_data,
    quartiles,
)


def _block(tag: str, body: str, fn: str = "fn") -> str:
    return (
        f"-------- [{tag}] {fn} /tmp/x.cpp:1 ---------\n"
        f"{body}\n"
        "------------------------------------------------\n"
    )


def _lemma(idx: int) -> str:
    """Minimal ~lemma_builder block parseable by both round.py (function
    name '~lemma_builder' triggers emission) and LemmaAnalyzer (which
    needs strategy + conclusion lines)."""
    body = f"strategy {idx}\n ==> j1 >= {idx}"
    return _block("nla_solver", body, fn="~lemma_builder")


# Three rounds, the third unclosed at trace end.
#
# Walk:
#   DEC 5,10                              current_level climbs to 10
#   NLA #1            → starts round 0 at (idx=1, N=10)
#   DEC 15            in-round; round 0 max → 15
#   NLA #2            in-round
#   AC  @18 conflict  in-round; counts toward round_length
#   DEC 20            in-round; round 0 max → 20
#   POP target=7      target<N(=10) → round-ending; climb=10, end_drop=3
#   DEC 12            current_level=12 (between rounds)
#   NLA #3            → starts round 1 at (idx=3, N=12); pop_marks[0]=(3,10,7)
#   DEC 15            in-round; round 1 max → 15
#   POP target=13     target≥N(=12) → internal pop; drop=5
#   POP target=8      target<N(=12) → round-ending; climb=3, end_drop=4
#   NLA #4            → starts round 2 at (idx=4, N=8);  pop_marks[1]=(4,12,8)
#   <trace ends, round 2 unclosed>
SYNTHETIC_TRACE = "".join(
    [
        _block("decide", "splitting, lvl: 5"),
        _block("decide", "splitting, lvl: 10"),
        _lemma(1),
        _block("decide", "splitting, lvl: 15"),
        _lemma(2),
        _block("arith_conflict", "@18 conflict"),
        _block("decide", "splitting, lvl: 20"),
        _block("pop_scope", "backtracking: 13, new_lvl: 7"),
        _block("decide", "splitting, lvl: 12"),
        _lemma(3),
        _block("decide", "splitting, lvl: 15"),
        _block("pop_scope", "backtracking: 5, new_lvl: 13"),
        _block("pop_scope", "backtracking: 10, new_lvl: 8"),
        _lemma(4),
    ]
)


def _write_trace(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return p


def test_parse_three_round_trace(tmp_path):
    p = _write_trace(tmp_path, "synthetic.trace", SYNTHETIC_TRACE)
    rd = parse_round_data(p)

    assert rd.label == "synthetic"
    assert rd.n_series == [10, 15, 12, 8]

    # round_starts has 3 entries (third round didn't close before trace end).
    assert rd.round_starts == [(1, 10), (3, 12), (4, 8)]
    # pop_marks has 2 entries — one per closed round.
    assert rd.pop_marks == [(3, 10, 7), (4, 12, 8)]

    # Pairing invariant: pop_marks[i] closes round_starts[i].
    for (start_idx, _start_N), (pop_idx, prev_N, _post) in zip(
        rd.round_starts, rd.pop_marks
    ):
        assert start_idx <= pop_idx
        assert prev_N == _start_N  # prev_N in pop_marks must match the round's anchor N

    assert rd.round_climbs == [10, 3]
    assert rd.internal_pop_drops == [5]
    assert rd.end_drops == [3, 4]
    assert rd.round_lengths == [4, 2]

    assert rd.totals == {
        "rounds": 3,
        "closed_rounds": 2,
        "nla_emissions": 4,
        "decisions": 6,
        "pops": 3,
        "arith_conflicts": 1,
    }
    assert rd.max_decision_level == 20


def test_empty_trace_warns(tmp_path, capsys):
    p = _write_trace(tmp_path, "empty.trace", "no trace blocks here\n")
    rd = parse_round_data(p)
    assert rd.n_series == []
    assert rd.round_starts == []
    assert rd.pop_marks == []
    captured = capsys.readouterr()
    assert "no NLA emissions" in captured.err


def test_false_case_of_check_nla_does_not_emit(tmp_path):
    """Only `~lemma_builder` counts as an emission. false_case_of_check_nla
    is a different nla_solver function (companion metadata); counting it
    as an emission would inflate n_series and break 1:1 cross-referencing
    with `lemur nla`'s lemma indices."""
    trace = "".join(
        [
            _block("decide", "splitting, lvl: 3"),
            _block("nla_solver", "false_case stuff", fn="false_case_of_check_nla"),
            _block("decide", "splitting, lvl: 7"),
            _lemma(1),
            _block("pop_scope", "backtracking: 4, new_lvl: 1"),
        ]
    )
    p = _write_trace(tmp_path, "alt.trace", trace)
    rd = parse_round_data(p)
    assert rd.n_series == [7]
    assert rd.totals["nla_emissions"] == 1


def test_pop_below_zero_round_anchor(tmp_path):
    """Round anchored at N=0 must still be detected; only target<N triggers
    round-end. target=0 with N=0 is a no-op (target>=N)."""
    trace = "".join(
        [
            _lemma(1),  # round 0 starts at N=0
            _block("decide", "splitting, lvl: 5"),
            _block("pop_scope", "backtracking: 5, new_lvl: 0"),  # target=N → internal
        ]
    )
    p = _write_trace(tmp_path, "z.trace", trace)
    rd = parse_round_data(p)
    assert rd.round_starts == [(1, 0)]
    assert rd.pop_marks == []  # no round-ending pop (target 0 not < N 0)
    assert rd.internal_pop_drops == [5]


def test_lemma_ranges_per_round(tmp_path):
    """Each round's lemma index range is derivable from RoundData."""
    p = _write_trace(tmp_path, "lr.trace", SYNTHETIC_TRACE)
    rd = parse_round_data(p)
    ranges = lemma_ranges_per_round(rd)

    # SYNTHETIC_TRACE walk:
    #   round 1: NLA #1, NLA #2, then closing pop @ next-emission idx 3
    #            → covers lemmas 1..2  (closed)
    #   round 2: NLA #3, then internal pop, then closing pop @ idx 4
    #            → covers lemma  3..3  (closed)
    #   round 3: NLA #4, trace ends
    #            → covers lemma  4..4  (open)
    assert ranges == [
        RoundLemmaRange(round=1, lemma_start=1, lemma_end=2, closed=True),
        RoundLemmaRange(round=2, lemma_start=3, lemma_end=3, closed=True),
        RoundLemmaRange(round=3, lemma_start=4, lemma_end=4, closed=False),
    ]


def test_lemma_ranges_per_round_empty_trace(tmp_path):
    """A trace with no NLA emissions yields no ranges."""
    p = _write_trace(tmp_path, "e.trace", "no entries\n")
    rd = parse_round_data(p, warn=False)
    assert lemma_ranges_per_round(rd) == []


def test_quartiles_empty_and_simple():
    assert quartiles([])["n"] == 0
    q = quartiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert q["n"] == 10
    assert q["min"] == 1
    assert q["max"] == 10
    assert q["mean"] == 5.5


def test_cli_json_format(tmp_path):
    """Smoke test: invoke `lemur n-over-time --format json` end-to-end."""
    p = _write_trace(tmp_path, "cli.trace", SYNTHETIC_TRACE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "n-over-time",
            str(p),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert "cli" in payload
    entry = payload["cli"]
    assert entry["totals"]["rounds"] == 3
    assert entry["totals"]["closed_rounds"] == 2
    assert entry["n_series"] == [10, 15, 12, 8]
    assert entry["round_starts"] == [[1, 10], [3, 12], [4, 8]]
    assert entry["pop_marks"] == [[3, 10, 7], [4, 12, 8]]


def test_cli_table_csv_to_file(tmp_path):
    """With --out PATH and --format table, CSV is written to file."""
    p = _write_trace(tmp_path, "csv.trace", SYNTHETIC_TRACE)
    out = tmp_path / "out.csv"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "n-over-time",
            str(p),
            "--format",
            "table",
            "--out",
            str(out),
        ],
        check=True,
    )
    text = out.read_text()
    assert "csv,rounds,3" in text
    assert "csv,nla_emissions,4" in text
    assert "round climb (max-N)" in text


def test_cli_nla_round_filter(tmp_path):
    """`lemur nla --round 1` keeps only the lemmas in round 1."""
    p = _write_trace(tmp_path, "r.trace", SYNTHETIC_TRACE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "nla",
            str(p),
            "--round",
            "1",
            "--list",
            "-f",
            "plain",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in result.stdout.strip().split("\n") if ln.strip()]
    # Round 1 covers lemmas 1..2 in SYNTHETIC_TRACE (strategy "strategy 1"
    # and "strategy 2"), renumbered 1.. after filter.
    assert len(lines) == 2
    assert lines[0].startswith("1.")
    assert lines[1].startswith("2.")
    assert "[round 1] lemmas 1-2" in result.stderr


def test_cli_nla_round_out_of_range(tmp_path):
    """Asking for a round that doesn't exist errors with the available list."""
    p = _write_trace(tmp_path, "r.trace", SYNTHETIC_TRACE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "nla",
            str(p),
            "--round",
            "99",
            "--list",
            "-f",
            "plain",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "round 99 not in trace" in result.stderr
    assert "Detected rounds: 1, 2, 3" in result.stderr


def test_cli_nla_by_round(tmp_path):
    """`lemur nla --by-round --list` groups output with round headers."""
    p = _write_trace(tmp_path, "br.trace", SYNTHETIC_TRACE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "nla",
            str(p),
            "--by-round",
            "--list",
            "-f",
            "plain",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout
    # Three round headers; round 3 is open (trace ended mid-round).
    assert "Round 1: lemmas 1–2" in out
    assert "Round 2: lemmas 3–3" in out
    assert "Round 3: lemmas 4–4" in out
    assert "open — trace ended mid-round" in out
    # Each round renders the lemma list inline.
    assert out.count("strategy") >= 4  # one strategy line per lemma


def test_cli_n_over_time_lemmas_per_round(tmp_path):
    """--lemmas-per-round augments the JSON output with per-round lemma details."""
    p = _write_trace(tmp_path, "lpr.trace", SYNTHETIC_TRACE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "n-over-time",
            str(p),
            "--format",
            "json",
            "--lemmas-per-round",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    rounds = payload["lpr"]["rounds"]
    assert len(rounds) == 3
    assert rounds[0]["round"] == 1
    assert rounds[0]["lemma_start"] == 1
    assert rounds[0]["lemma_end"] == 2
    assert rounds[0]["closed"] is True
    assert len(rounds[0]["lemmas"]) == 2
    assert rounds[0]["lemmas"][0]["index"] == 1
    assert rounds[0]["lemmas"][0]["strategy"] == "strategy"
    assert rounds[2]["closed"] is False  # final round didn't close


def test_cli_nla_no_truncate(tmp_path):
    """`lemur nla --detail N --no-truncate` shows the full SMT name from the
    varmap; without the flag the substituted text is truncated to 40 chars."""
    long_smt = "(div (if (or #2084 #1822) (+ R12 R34 R56 R78 R90 R12) R0) 2)"
    assert len(long_smt) > 40
    # Trace: a varmap line maps j1 → long_smt, and a ~lemma_builder block's
    # conclusion references j1.
    trace = "".join(
        [
            _block("nla_solver", f"varmap: j1=1: {long_smt}", fn="check"),
            _block("nla_solver", "strategy 1\n ==> j1 >= 1", fn="~lemma_builder"),
        ]
    )
    p = _write_trace(tmp_path, "long.trace", trace)

    # Default: truncated.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "nla",
            str(p),
            "--detail",
            "1",
            "-f",
            "plain",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "..." in result.stdout
    assert long_smt not in result.stdout

    # --no-truncate: full text.
    result_full = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "nla",
            str(p),
            "--detail",
            "1",
            "--no-truncate",
            "-f",
            "plain",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert long_smt in result_full.stdout


def test_cli_lemmas_per_round_requires_json(tmp_path):
    """--lemmas-per-round + --format table is rejected."""
    p = _write_trace(tmp_path, "x.trace", SYNTHETIC_TRACE)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lemur.cli.main",
            "n-over-time",
            str(p),
            "--format",
            "table",
            "--lemmas-per-round",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "only applies with --format json" in result.stderr
