#!/usr/bin/env python3
"""Entrypoint for the Deriv live-data trading terminal."""

from live_server import AutobotScheduler, JarvisHandler, main, run_server

__all__ = ["AutobotScheduler", "JarvisHandler", "main", "run_server"]


if __name__ == "__main__":
    raise SystemExit(main())
