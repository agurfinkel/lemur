"""Lossless framing of TRACE/CTRACE output for JSONL consumers.

This is separate from parse_trace: existing report parsers retain their behavior.
Offsets are zero-based bytes, end exclusive; trace line numbers are one-based.
A record buffers one framed block or one contiguous run of unframed lines.
"""

from collections.abc import Iterator
from typing import BinaryIO

from lemur.parsers import FOOTER_RE, HEADER_RE

SCHEMA_VERSION = 1


def trace_events(source: BinaryIO) -> Iterator[dict]:
    """Yield ordered, JSON-compatible records, preserving every input byte.

    Concatenate record['text'].encode('utf-8', 'surrogateescape') to recover
    the input. JSON must escape non-ASCII characters to preserve invalid UTF-8.
    A new header interrupts an unfinished block; no guessed tag is assigned
    to unframed output. Record IDs are local to this input stream.
    """
    seq = 0
    offset = 0
    line_number = 0
    start_offset = 0
    start_line = 1
    chunks = []
    header = None

    def record(complete: bool, reason: str | None = None) -> dict:
        nonlocal seq
        seq += 1
        event = {
            "schema": SCHEMA_VERSION,
            "kind": "trace" if header else "raw",
            "seq": seq,
            "span": {
                "byte_start": start_offset,
                "byte_end": start_offset + sum(map(len, chunks)),
                "line_start": start_line,
                "line_end": start_line + len(chunks) - 1,
            },
            "text": b"".join(chunks).decode("utf-8", "surrogateescape"),
        }
        if header:
            event.update(
                tag=header.group(1),
                function=header.group(2),
                source_file=header.group(3),
                source_line=int(header.group(4)),
                complete=complete,
            )
            if reason:
                event["reason"] = reason
        return event

    for raw in source:
        line_number += 1
        line = raw.decode("utf-8", "surrogateescape").rstrip("\r\n")
        match = HEADER_RE.fullmatch(line)
        if match:
            if chunks:
                yield record(False, "next-header" if header else None)
            chunks = []
            header = match
            start_offset = offset
            start_line = line_number
        elif not chunks:
            start_offset = offset
            start_line = line_number
        chunks.append(raw)
        offset += len(raw)
        if header and FOOTER_RE.fullmatch(line):
            yield record(True)
            chunks = []
            header = None
    if chunks:
        yield record(False, "eof" if header else None)


def event_body(event: dict) -> str:
    """Return a framed record's body without changing the preserved text."""
    lines = event["text"].splitlines(keepends=True)
    return "".join(lines[1:-1] if event.get("complete") else lines[1:])
