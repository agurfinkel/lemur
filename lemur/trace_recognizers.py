"""Optional domain recognizers layered over the generic event stream.

Recognizers consume events in order, may retain state, and emit derived records
with event_ids pointing to their evidence. finish() can report pending matches.
Original records always pass through unchanged. Unknown shapes stay raw.
"""

import re
from collections.abc import Iterable, Iterator
from typing import Protocol

from lemur.trace_stream import SCHEMA_VERSION, event_body


class Recognizer(Protocol):
    name: str
    version: int

    def feed(self, event: dict) -> Iterable[dict]: ...

    def finish(self) -> Iterable[dict]: ...


class BVRecognizer:
    """Conservative recognition of a few BV/SAT trace sites, not a cost model.

    Matched by tag, function, source filename and body shape. Printed SAT
    literals remain strings: their encoding and scope belong to Z3.
    """

    name = "bv"
    version = 1

    def feed(self, event: dict) -> Iterable[dict]:
        if event["kind"] != "trace" or not event.get("complete"):
            return []
        body = event_body(event).strip()
        tag, fn = event["tag"], event["function"]
        source = event["source_file"].replace("\\", "/").rsplit("/", 1)[-1]
        data = None
        if (
            tag == "bit_blaster"
            and fn == "reduce_app"
            and source == "bit_blaster_rewriter.cpp"
        ):
            match = re.match(r"([^\s()]+)(?:\s|$)", body)
            if match:
                data = {"event": "bitblast_application", "operator": match[1]}
        elif (
            tag == "bv" and fn == "internalize_term_core" and source == "theory_bv.cpp"
        ):
            match = re.fullmatch(r"internalizing term: #(\d+) (.+)", body, re.DOTALL)
            if match:
                data = {
                    "event": "bv_internalize",
                    "expr_id": int(match[1]),
                    "expression_text": match[2],
                }
        elif tag == "sat_decide" and fn == "decide" and source == "sat_solver.cpp":
            match = re.fullmatch(r"(\d+): next-case-split: (\S+)", body)
            if match:
                data = {
                    "event": "sat_decision",
                    "level": int(match[1]),
                    "literal": match[2],
                }
        elif tag == "sat" and fn == "mk_clause_core" and source == "sat_solver.cpp":
            match = re.fullmatch(r"mk_clause: (.*?) (learned|aux)", body, re.DOTALL)
            if match:
                data = {
                    "event": "sat_clause",
                    "role": match[2],
                    "literals_text": match[1],
                }
        if data is None:
            return []
        return [{"event_ids": [event["seq"]], "data": data}]

    def finish(self) -> Iterable[dict]:
        return []


def recognize(
    events: Iterable[dict], recognizers: Iterable[Recognizer]
) -> Iterator[dict]:
    """Pass through evidence and interleave annotations; flush state at EOF."""
    recognizers = list(recognizers)

    def annotation(recognizer, match):
        return {
            "schema": SCHEMA_VERSION,
            "kind": "recognition",
            "recognizer": recognizer.name,
            "recognizer_version": recognizer.version,
            **match,
        }

    for event in events:
        yield event
        for recognizer in recognizers:
            for match in recognizer.feed(event):
                yield annotation(recognizer, match)
    for recognizer in recognizers:
        for match in recognizer.finish():
            yield annotation(recognizer, match)
