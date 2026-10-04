"""Bandwidth watcher: quota, expiry, panel health, and snapshots.

``BWatch`` is the only public name. Feature methods live on mixins; callers
keep importing ``from bwatch import BWatch``.
"""

from __future__ import annotations

from .watch import BWatch

__all__ = ["BWatch"]
