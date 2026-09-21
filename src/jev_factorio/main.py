"""CLI: python -m jev_factorio --backend mock --steps 8"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from .backends.mock import MockBackend
from .loop import AgentLoop


def make_backend(name: str, resume: bool = False):
    if name == "mock":
        return MockBackend()
    if name == "play_api":
        from .backends.play_api import PlayApiBackend
        return PlayApiBackend(factorio_user_dir=os.environ.get(
            "FACTORIO_USER_DIR", "~/.factorio"))
    if name == "fle":
        from .backends.fle import FleBackend
        b = FleBackend()
        b.start(resume=resume)
        return b
    raise SystemExit(f"unknown backend: {name}")


def cli() -> None:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    p = argparse.ArgumentParser(prog="jev-factorio")
    p.add_argument("--backend", default=os.environ.get("JEV_BACKEND", "mock"))
    limits = p.add_mutually_exclusive_group()
    limits.add_argument("--steps", type=int)
    limits.add_argument("--duration-hours", type=float)
    p.add_argument("--resume", action="store_true",
                   help="Resume an existing live FLE session without resetting its world")
    p.add_argument("--tick-seconds", type=float,
                   default=float(os.environ.get("JEV_TICK_SECONDS", "0")))  # 0 in mock
    p.add_argument("--confidence-floor", type=float,
                   default=float(os.environ.get("JEV_CONFIDENCE_FLOOR", "0.45")))
    p.add_argument("--log-file", default=os.environ.get("JEV_LOG_FILE"))
    args = p.parse_args()
    if args.duration_hours is not None and (
        not 0 < args.duration_hours < float("inf")
    ):
        p.error("--duration-hours must be finite and positive")
    if args.tick_seconds < 0 or not args.tick_seconds < float("inf"):
        p.error("--tick-seconds must be finite and nonnegative")
    if args.resume and args.backend != "fle":
        p.error("--resume requires --backend fle")
    loop = AgentLoop(make_backend(args.backend, resume=args.resume),
              confidence_floor=args.confidence_floor,
              tick_seconds=args.tick_seconds,
              log_file=args.log_file)
    if args.duration_hours is not None:
        loop.run(steps=None, duration_seconds=args.duration_hours * 3600)
    else:
        loop.run(steps=args.steps if args.steps is not None else 8)


if __name__ == "__main__":
    cli()
