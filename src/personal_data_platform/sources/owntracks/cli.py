"""OwnTracks HTTP receiver commands."""

import argparse


def configure(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="owntracks_command", required=True)
    commands.add_parser("serve", help="receive authenticated OwnTracks locations")
    commands.add_parser("serve-worker", help="load authenticated GCS finalize notifications")


def run(args: argparse.Namespace) -> int:
    from .runtime import run_server

    return run_server(worker=args.owntracks_command == "serve-worker")
