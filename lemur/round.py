"""
Round-based level analysis of a z3 trace.

A "round" begins when an NLA emission lands at the current SAT decision
level N (after either trace start or a prior round-ending pop). The round
runs while N climbs (NLA lemmas drive new decisions); it ends when a
pop_scope brings the level *below* the round's anchor N. The crossing
depth is N - target.

Shared core for `lemur n-over-time`. The same RoundData backs the plot
renderers (html/png) and the stats renderers (table/json).

Required trace tags. Capture with::

    lemur sweep --trace nla_solver,decide,pop_scope,arith_conflict --save DIR/

or directly::

    z3 -tr:decide -tr:pop_scope -tr:nla_solver -tr:arith_conflict ...

Caveats: `current_level` is an aggregate of original-formula-atom decisions
AND NLA-fresh-literal decisions accumulated from earlier rounds, not a
clean SAT-trail depth. See round-analysis caveats in z3-research notes.
"""

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lemur.parsers import parse_trace

_DEC_RE = re.compile(r"splitting, lvl:\s*(\d+)")
_POP_RE = re.compile(r"backtracking:\s*(\d+),\s*new_lvl:\s*(\d+)")
_AC_RE = re.compile(r"@(\d+)\s+(conflict|lemma)")


@dataclass
class RoundData:
    """One trace's worth of round-level analysis.

    ``round_starts`` and ``pop_marks`` use 1-based emission indices into
    ``n_series`` (so the i-th round-start sits at x=round_starts[i][0] on
    the plot's x-axis).

    Pairing: ``pop_marks[i]`` is the pop that ended ``round_starts[i]``.
    If the trace ends mid-round, ``len(round_starts) == len(pop_marks)+1``
    — the trailing round_starts entry has no closing pop_mark.
    """

    label: str
    n_series: list[int] = field(default_factory=list)
    round_starts: list[tuple[int, int]] = field(default_factory=list)
    pop_marks: list[tuple[int, int, int]] = field(default_factory=list)
    round_climbs: list[int] = field(default_factory=list)
    internal_pop_drops: list[int] = field(default_factory=list)
    end_drops: list[int] = field(default_factory=list)
    round_lengths: list[int] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    max_decision_level: int = 0


def parse_round_data(
    trace_path: str | Path,
    label: str | None = None,
    *,
    warn: bool = True,
) -> RoundData:
    """Single streaming pass over the trace; produces a full RoundData.

    If ``warn`` and the trace yields no NLA emissions, prints a hint to
    stderr that the required ``-tr:`` tags may be missing.
    """
    path = Path(trace_path)
    rd = RoundData(label=label if label is not None else path.stem)
    counts: Counter[str] = Counter()
    max_dec = 0

    current_level = 0
    round_N: int | None = None
    round_max_level = 0
    round_event_count = 0
    last_pop_below_N: tuple[int, int] | None = None  # (prev_N, post)

    # Single pass over the structured trace. parse_trace handles
    # header/footer detection; we match on tag + function so that
    # `entry.function == '~lemma_builder'` is the same gate
    # `LemmaAnalyzer` uses — emission_idx here lines up 1:1 with
    # lemma indices in `lemur nla --list`.
    for entry in parse_trace(path):
        if entry.tag == "decide":
            m = _DEC_RE.search(entry.body)
            if m:
                current_level = int(m.group(1))
                counts["DEC"] += 1
                if current_level > max_dec:
                    max_dec = current_level
                if round_N is not None:
                    round_event_count += 1
                    if current_level > round_max_level:
                        round_max_level = current_level
        elif entry.tag == "pop_scope":
            m = _POP_RE.search(entry.body)
            if m:
                drop = int(m.group(1))
                target = int(m.group(2))
                counts["POP"] += 1
                if round_N is not None:
                    if target >= round_N:
                        rd.internal_pop_drops.append(drop)
                        round_event_count += 1
                    else:
                        rd.round_climbs.append(round_max_level - round_N)
                        rd.end_drops.append(round_N - target)
                        rd.round_lengths.append(round_event_count)
                        last_pop_below_N = (round_N, target)
                        round_N = None
                        round_max_level = 0
                        round_event_count = 0
                current_level = target
        elif entry.tag == "arith_conflict":
            m = _AC_RE.search(entry.body)
            if m:
                counts["AC"] += 1
                if round_N is not None:
                    round_event_count += 1
        elif entry.tag == "nla_solver" and entry.function == "~lemma_builder":
            rd.n_series.append(current_level)
            counts["NLA"] += 1
            emission_idx = len(rd.n_series)
            if last_pop_below_N is not None:
                prev_N, post = last_pop_below_N
                rd.pop_marks.append((emission_idx, prev_N, post))
                last_pop_below_N = None
            if round_N is None:
                round_N = current_level
                round_max_level = current_level
                round_event_count = 0
                rd.round_starts.append((emission_idx, current_level))
            else:
                round_event_count += 1

    rd.totals = {
        "rounds": len(rd.round_starts),
        "closed_rounds": len(rd.pop_marks),
        "nla_emissions": counts["NLA"],
        "decisions": counts["DEC"],
        "pops": counts["POP"],
        "arith_conflicts": counts["AC"],
    }
    rd.max_decision_level = max_dec

    if warn and not rd.n_series:
        print(
            f"warning: no NLA emissions in {path}. "
            f"capture with `-tr:decide -tr:pop_scope -tr:nla_solver "
            f"-tr:arith_conflict`.",
            file=sys.stderr,
        )

    return rd


@dataclass
class RoundLemmaRange:
    """1-based lemma index range covered by one round.

    `lemma_start` and `lemma_end` are inclusive 1-based indices into the
    LemmaAnalyzer's record list (which sees only ~lemma_builder blocks
    in trace order — the same numbering `lemur nla --list` uses).
    `closed` is True when a round-ending pop bounded the round; False
    means the trace ended mid-round and `lemma_end` is the last lemma
    in the trace.
    """

    round: int  # 1-based
    lemma_start: int  # 1-based inclusive
    lemma_end: int  # 1-based inclusive
    closed: bool


def lemma_ranges_per_round(rd: RoundData) -> list[RoundLemmaRange]:
    """Slice the lemma stream by round boundaries.

    Each round_starts[i] anchors round i+1 starting at lemma index
    round_starts[i][0]. The next round's anchor (or trace end) bounds it.
    pop_marks[i][0] is the FIRST lemma of round i+1 (i.e. the lemma that
    began the round AFTER the round-ending pop), so round i ends at
    lemma index pop_marks[i][0] - 1.
    """
    out: list[RoundLemmaRange] = []
    last_lemma = len(rd.n_series)
    for i, (start_idx, _anchor_N) in enumerate(rd.round_starts):
        if i < len(rd.pop_marks):
            end_idx = rd.pop_marks[i][0] - 1
            closed = True
        else:
            end_idx = last_lemma
            closed = False
        # Only emit non-empty ranges. (start_idx > end_idx would happen if
        # a round started but the very next line was its closing pop with
        # no further lemmas — shouldn't occur given our state machine,
        # but guard anyway.)
        if end_idx >= start_idx:
            out.append(
                RoundLemmaRange(
                    round=i + 1,
                    lemma_start=start_idx,
                    lemma_end=end_idx,
                    closed=closed,
                )
            )
    return out


def quartiles(xs: list[int]) -> dict[str, float | int]:
    """min/q1/median/q3/max/mean for a list of ints. Empty → all 0."""
    if not xs:
        return {"n": 0, "min": 0, "q1": 0, "median": 0, "q3": 0, "max": 0, "mean": 0.0}
    s = sorted(xs)
    n = len(s)
    return {
        "n": n,
        "min": s[0],
        "q1": s[n // 4],
        "median": s[n // 2],
        "q3": s[3 * n // 4],
        "max": s[-1],
        "mean": round(sum(s) / n, 2),
    }
