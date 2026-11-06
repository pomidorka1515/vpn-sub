from __future__ import annotations

import fcntl
import os
import re
from datetime import UTC, datetime
from pathlib import Path


def publish_backup(temporary: str, instance_dir: str, suffix: str) -> Path:
    """Publish complete snapshots in a persistent, cross-writer order."""
    directory = Path(instance_dir)
    # Keep the lock file in place: replacing it would let writers lock different inodes.
    with (directory / ".backup-order").open("a+", encoding="utf-8") as order:
        fcntl.flock(order, fcntl.LOCK_EX)
        try:
            order.seek(0)
            previous = order.read().strip()
            names = [path.stem for path in directory.glob(f"*{suffix}")]
            names.append(previous)
            keys = [
                (match[1], int(match[2] or 0))
                for name in names
                if (match := re.fullmatch(r"(\d{8}-\d{6})(?:_(\d+))?", name))
            ]
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            latest_timestamp, sequence = max(keys, default=(timestamp, 0))
            if timestamp > latest_timestamp:
                sequence = 0
            else:
                timestamp = latest_timestamp
            while True:
                sequence += 1
                destination = directory / f"{timestamp}_{sequence:020d}{suffix}"
                try:
                    destination.hardlink_to(temporary)
                    break
                except FileExistsError:
                    continue
            order.seek(0)
            order.truncate()
            order.write(destination.stem)
            order.flush()
            os.fsync(order.fileno())
            Path(temporary).unlink()
            return destination
        finally:
            fcntl.flock(order, fcntl.LOCK_UN)
