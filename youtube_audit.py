#!/usr/bin/env python3
"""Stable root entrypoint for the production GitHub Actions runtime."""
from runtime.youtube_audit import main
if __name__ == "__main__":
    raise SystemExit(main())
