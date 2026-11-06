"""
SMT2 text-level injection helpers.

Shared by `lemur sweep --split` and `lemur split`. Kept separate from
`lemur/sweep.py` so that `lemur split` can import just the injection
helper without pulling in the process-pool orchestration machinery.
"""

from pathlib import Path


def make_split_smt(src_path: str, inject: str, dest_path: str) -> None:
    """Write a copy of `src_path` with `inject` placed before the first
    non-commented `(check-sat)`. If no `(check-sat)` is found, one is
    appended along with the injection."""
    content = Path(src_path).read_text()
    lines = content.split("\n")
    out_lines: list[str] = []
    injected = False
    for line in lines:
        code = line
        if ";" in code:
            code = code[: code.index(";")]
        if not injected and "(check-sat)" in code:
            out_lines.append("; --- lemur split injection ---")
            out_lines.append(inject)
            out_lines.append("; --- end injection ---")
            injected = True
        out_lines.append(line)
    if not injected:
        out_lines.append("; --- lemur split injection ---")
        out_lines.append(inject)
        out_lines.append("(check-sat)")
    Path(dest_path).write_text("\n".join(out_lines))
