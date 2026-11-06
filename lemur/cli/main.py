"""lemur: Z3 trace analysis and debugging toolkit."""

import argparse
import sys

from lemur.cli import (
    agent_help,
    export,
    nla,
    search,
    split_status,
    stats,
    stats_compare,
    sweep,
    tally,
)
from lemur.cli import n_over_time as n_over_time_cli
from lemur.cli import nla_diff as nla_diff_cli
from lemur.cli import nla_run as nla_run_cli
from lemur.cli import sdiff as sdiff_cli
from lemur.cli import sgrep as sgrep_cli
from lemur.cli import split as split_cli
from lemur.cli import stats_diff as stats_diff_cli


def main():
    parser = argparse.ArgumentParser(
        prog="lemur",
        description="Z3 trace analysis and debugging toolkit.",
        epilog="AI agents: use --agent (on any subcommand) for terse usage guide.",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Show agent-friendly usage guide (all subcommands)",
    )
    sub = parser.add_subparsers(dest="command")

    export.register(sub)
    sweep.register(sub)
    stats.register(sub)
    nla.register(sub)
    tally.register(sub)
    stats_compare.register(sub)
    search.register(sub)
    split_cli.register(sub)
    split_status.register(sub)
    sgrep_cli.register(sub)
    sdiff_cli.register(sub)
    nla_diff_cli.register(sub)
    nla_run_cli.register(sub)
    stats_diff_cli.register(sub)
    n_over_time_cli.register(sub)

    args, remaining = parser.parse_known_args()

    if args.agent:
        print(agent_help.full())
        sys.exit(0)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Re-parse fully now that we know it's not top-level --agent
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
