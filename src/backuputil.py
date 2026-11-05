from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def publish_backup(temporary: str, instance_dir: str, suffix: str) -> Path:
    """Publish a complete snapshot without replacing any existing backup."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    directory = Path(instance_dir)
    # Start after existing sequences, including when retention removed earlier ones.
    sequences = (
        path.stem.removeprefix(f"{timestamp}_")
        for path in directory.glob(f"{timestamp}_*{suffix}")
    )
    sequence = max((int(value) for value in sequences if value.isdecimal()), default=0) + 1
    while True:
        # '_' sorts after '.', keeping legacy second-only backups before new ones.
        destination = directory / f"{timestamp}_{sequence:020d}{suffix}"
        try:
            # Both paths are on the same filesystem. Linking publishes only complete
            # snapshots and fails atomically if another writer claimed this name.
            destination.hardlink_to(temporary)
        except FileExistsError:
            sequence += 1
            continue
        Path(temporary).unlink()
        return destination
