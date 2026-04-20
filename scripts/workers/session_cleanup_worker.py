"""Standalone session cleanup worker."""

from __future__ import annotations

from backend.api import run_session_cleanup_forever


if __name__ == "__main__":
    run_session_cleanup_forever()
