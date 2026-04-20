"""Standalone online shadow evaluation worker."""

from __future__ import annotations

from core.services import get_default_online_shadow_eval


if __name__ == "__main__":
    get_default_online_shadow_eval().run_worker_forever()
