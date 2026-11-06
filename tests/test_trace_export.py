import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lemur.trace_recognizers import BVRecognizer, recognize
from lemur.trace_stream import trace_events


def block(body=b"payload\n", tag=b"unknown", footer=True):
    return (
        b"-------- ["
        + tag
        + b"] f source.cpp:7 ---------\n"
        + body
        + (b"-" * 48 + b"\n" if footer else b"")
    )


def decode(raw):
    records = list(trace_events(io.BytesIO(raw)))
    # Exercise actual JSON serialization, including surrogate-escaped bytes.
    return [json.loads(json.dumps(r, ensure_ascii=True)) for r in records]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"unframed\n",
        b"no newline",
        b"\xff\x00\n",
        block(),
        block().replace(b"\n", b"\r\n"),
        block(footer=False),
        block(b"no final newline", footer=False),
        b"prefix\n" + block() + b"\nSTRACE text\n" + block(b"tail\xff", footer=False),
        block(footer=False) + block(),
        b"-" * 48 + b"\n",
    ],
)
def test_lossless_partition(raw):
    records = decode(raw)
    assert (
        b"".join(r["text"].encode("utf-8", "surrogateescape") for r in records) == raw
    )
    cursor = 0
    for seq, record in enumerate(records, 1):
        span = record["span"]
        assert record["seq"] == seq
        assert span["byte_start"] == cursor
        assert raw[span["byte_start"] : span["byte_end"]] == record["text"].encode(
            "utf-8", "surrogateescape"
        )
        assert span["line_start"] == raw[:cursor].count(b"\n") + 1
        cursor = span["byte_end"]
        assert span["line_end"] == raw[:cursor].count(b"\n") + (
            not raw[:cursor].endswith(b"\n")
        )
    assert cursor == len(raw)


def test_incomplete_and_unframed_are_explicit():
    records = decode(b"outside\n" + block(footer=False) + block(b"last", footer=False))
    assert [r["kind"] for r in records] == ["raw", "trace", "trace"]
    assert "tag" not in records[0]
    assert [(r["complete"], r["reason"]) for r in records[1:]] == [
        (False, "next-header"),
        (False, "eof"),
    ]


def test_unknown_tags_and_source_paths_are_preserved():
    raw = block(tag=b"new-tag").replace(b"source.cpp", b"C:\\source tree\\file.cpp")
    event = decode(raw)[0]
    assert event["tag"] == "new-tag"
    assert event["source_file"] == "C:\\source tree\\file.cpp"
    assert event["source_line"] == 7
    assert event["complete"]


def test_yields_before_reading_whole_input():
    def source():
        yield from block().splitlines(keepends=True)
        raise AssertionError("read ahead past complete record")

    assert next(trace_events(source()))["complete"]


def test_real_bv_trace_sites():
    fixture = Path(__file__).parent / "sample_traces" / "bv_export.trace"
    with fixture.open("rb") as source:
        events = list(trace_events(source))
    records = list(recognize(events, [BVRecognizer()]))
    assert [r for r in records if r["kind"] != "recognition"] == events
    annotations = [r for r in records if r["kind"] == "recognition"]
    assert {r["data"]["event"] for r in annotations} == {
        "bitblast_application",
        "bv_internalize",
        "sat_clause",
        "sat_decision",
    }
    by_id = {r["seq"]: r for r in events}
    for r in annotations:
        assert r["recognizer"] == "bv"
        assert r["recognizer_version"] == 1
        assert by_id[r["event_ids"][0]]["complete"]
    decision = next(
        r["data"] for r in annotations if r["data"]["event"] == "sat_decision"
    )
    assert decision == {"event": "sat_decision", "level": 1, "literal": "-35"}
    # Serialized generic events are sufficient for later recognizer layers.
    assert list(recognize(json.loads(json.dumps(events)), [BVRecognizer()])) == records
    for event in events:
        altered = dict(event, complete=False)
        assert list(BVRecognizer().feed(altered)) == []
        altered = dict(event, source_file="different.cpp")
        assert list(BVRecognizer().feed(altered)) == []


def test_stateful_recognizer_can_join_events_and_flush():
    class Pair:
        name = "pair"
        version = 1
        pending = None

        def feed(self, event):
            if self.pending is None:
                self.pending = event["seq"]
                return []
            ids = [self.pending, event["seq"]]
            self.pending = None
            return [{"event_ids": ids, "data": {"complete": True}}]

        def finish(self):
            return (
                []
                if self.pending is None
                else [{"event_ids": [self.pending], "data": {"complete": False}}]
            )

    events = decode(block() * 3)
    output = list(recognize(events, [Pair()]))
    matches = [r for r in output if r["kind"] == "recognition"]
    assert [r["event_ids"] for r in matches] == [[1, 2], [3]]
    assert [r["data"]["complete"] for r in matches] == [True, False]
    assert [r for r in output if r["kind"] != "recognition"] == events


def cli(*args, data=None):
    return subprocess.run(
        [sys.executable, "-m", "lemur.cli.main", "export", *map(str, args)],
        input=data,
        capture_output=True,
    )


def test_cli_stdin_and_agent_help():
    raw = b"outside\xff\n" + block(footer=False)
    result = cli("-", data=raw)
    assert result.returncode == 0
    assert not result.stderr
    assert (
        b"".join(
            json.loads(line)["text"].encode("utf-8", "surrogateescape")
            for line in result.stdout.splitlines()
        )
        == raw
    )
    result = cli("--agent")
    assert result.returncode == 0
    assert b"Sea-MLIR / BV exploration" in result.stdout
    assert b"--recognizer bv" in result.stdout


def test_cli_file_and_input_protection(tmp_path):
    source, target = tmp_path / "trace", tmp_path / "events.jsonl"
    source.write_bytes(block())
    result = cli(source, "-o", target)
    assert result.returncode == 0
    assert not result.stdout
    assert json.loads(target.read_text())["complete"]
    assert cli(source, "-o", source).returncode == 2
    assert source.read_bytes() == block()
    alias = tmp_path / "alias"
    alias.hardlink_to(source)
    assert cli(source, "-o", alias).returncode == 2
    assert source.read_bytes() == block()
    assert cli(tmp_path / "missing").returncode == 2
    assert cli(source, "--recognizer", "missing").returncode == 2
