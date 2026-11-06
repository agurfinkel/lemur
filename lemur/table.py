"""
Output formatting for lemur tools.

Provides dual-mode output:
- Rich tables/panels for human consumption (default when TTY)
- Plain/JSON for machine consumption (--format plain|json, or non-TTY default)
"""

import csv
import io
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def make_console(force_terminal: bool | None = None, no_color: bool = False) -> Console:
    """Create a Rich console with appropriate settings."""
    return Console(
        force_terminal=force_terminal,
        no_color=no_color,
        stderr=False,
    )


def is_tty() -> bool:
    return sys.stdout.isatty()


class SweepTable:
    """Formats sweep results as a config x seed table."""

    def __init__(self, configs: list[str], seeds: list[int]):
        self.configs = configs
        self.seeds = seeds
        # results[config_name][seed] = (status, time_s)
        self.results: dict[str, dict[int, tuple[str, float]]] = {c: {} for c in configs}

    def add_result(self, config: str, seed: int, status: str, time_s: float):
        self.results[config][seed] = (status, time_s)

    def _status_style(self, status: str) -> str:
        if status == "sat":
            return "green"
        elif status == "unsat":
            return "cyan"
        elif status == "timeout":
            return "red"
        elif status == "unknown":
            return "yellow"
        return "white"

    def _cell(self, status: str, time_s: float) -> Text:
        style = self._status_style(status)
        return Text(f"{status} {time_s:.1f}s", style=style)

    def render_rich(self, console: Console):
        table = Table(title="Sweep Results", show_lines=True, pad_edge=True)
        table.add_column("Config", style="bold", no_wrap=True)
        for seed in self.seeds:
            table.add_column(f"s{seed}", justify="center", min_width=12)
        table.add_column("Solved", justify="right", style="bold")

        for config in self.configs:
            row = []
            solved = 0
            for seed in self.seeds:
                if seed in self.results[config]:
                    status, time_s = self.results[config][seed]
                    row.append(self._cell(status, time_s))
                    if status in ("sat", "unsat"):
                        solved += 1
                else:
                    row.append(Text("—", style="dim"))
            total = len(self.seeds)
            row.append(
                Text(
                    f"{solved}/{total}",
                    style="bold green" if solved == total else "bold",
                )
            )
            table.add_row(config, *row)

        console.print(table)

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["config", "seed", "status", "time_s"])
        for config in self.configs:
            for seed in self.seeds:
                if seed in self.results[config]:
                    status, time_s = self.results[config][seed]
                    writer.writerow([config, seed, status, f"{time_s:.3f}"])
        return buf.getvalue()

    def to_json(self) -> str:
        rows = []
        for config in self.configs:
            for seed in self.seeds:
                if seed in self.results[config]:
                    status, time_s = self.results[config][seed]
                    rows.append(
                        {
                            "config": config,
                            "seed": seed,
                            "status": status,
                            "time_s": round(time_s, 3),
                        }
                    )
        return json.dumps(rows, indent=2)


class StatsOutput:
    """Formats trace statistics for display."""

    def __init__(self):
        self.sections: list[tuple[str, list[tuple[str, str]]]] = []

    def add_section(self, title: str, rows: list[tuple[str, str]]):
        """Add a named section with key-value rows."""
        self.sections.append((title, rows))

    def render_rich(self, console: Console):
        for title, rows in self.sections:
            table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
            table.add_column("Key", style="bold")
            table.add_column("Value")
            for key, value in rows:
                table.add_row(key, value)
            from rich.text import Text as RichText

            panel_title = RichText(title, style="bold")
            console.print(Panel(table, title=panel_title, expand=False))

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["section", "key", "value"])
        for title, rows in self.sections:
            for key, value in rows:
                writer.writerow([title, key, value])
        return buf.getvalue()

    def to_json(self) -> str:
        data = {}
        for title, rows in self.sections:
            data[title] = {key: value for key, value in rows}
        return json.dumps(data, indent=2)


def output(
    obj: SweepTable | StatsOutput,
    fmt: str | None = None,
    console: Console | None = None,
):
    """Render output in the requested format.

    fmt: 'rich' (default for TTY), 'plain', 'json'
    """
    if fmt is None:
        fmt = "rich" if is_tty() else "plain"

    if fmt == "rich":
        if console is None:
            console = make_console()
        obj.render_rich(console)
    elif fmt == "plain":
        print(obj.to_csv(), end="")
    elif fmt == "json":
        print(obj.to_json())
    else:
        raise ValueError(f"Unknown format: {fmt}")
