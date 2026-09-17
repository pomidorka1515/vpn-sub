import os
import shutil
import json
import tempfile
import glob
import threading

from datetime import datetime, timezone

from ._jsonc import _strip_jsonc_comments, _strip_jsonc_trailing_commas
from ._constants import CONFIG_TYPES

from loggers import Logger

def _instance_backup_dir(path: str, backup_dir: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(backup_dir, name)

def _do_backup(
    path: str,
    indent: int,
    instance_dir: str,
    log: Logger,
    *,
    minify: bool = False,
    raw: bool = False,
    jsonc: bool = False,
) -> None:
    """Take a snapshot and write it to a per-instance subdirectory.

    Args:
        raw: When True (used by LinesConfig) the file is copied byte-for-byte
             instead of being parsed and re-serialised as JSON.  This is required
             because JSONL files are not valid single-document JSON and
             json.load() would raise JSONDecodeError on them.
        jsonc: When True, parse JSONC comments and trailing commas before
               writing the normalized backup.
    """
    os.makedirs(instance_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if raw:
        backup_path = os.path.join(instance_dir, f"{timestamp}.jsonl")
        fd, tmp = tempfile.mkstemp(dir=instance_dir, prefix=".tmp-", suffix=".tmp")
        os.close(fd)
        try:
            shutil.copy2(path, tmp)
            os.replace(tmp, backup_path)
        except FileNotFoundError:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            return
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    else:
        backup_path = os.path.join(instance_dir, f"{timestamp}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if jsonc:
                content = _strip_jsonc_comments(content)
                content = _strip_jsonc_trailing_commas(content)
            data = json.loads(content)
        except FileNotFoundError:
            return
        except json.JSONDecodeError as e:
            if jsonc:
                log.warning(f"skipping backup: failed to parse JSONC in {path}: {e}")
            return
        fd, tmp = tempfile.mkstemp(dir=instance_dir, prefix=".tmp-", suffix=".tmp")
        os.close(fd)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                if minify:
                    json.dump(data, f, indent=None, separators=(',', ':'), ensure_ascii=False)
                else:
                    json.dump(data, f, indent=indent, ensure_ascii=False)
            os.replace(tmp, backup_path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    log.debug(f"backup saved: {backup_path}")

def _prune_backups(
    instance_dir: str,
    retention: int,
    log: Logger,
    config_type: CONFIG_TYPES
) -> None:
    """Keep only the N most recent backups."""
    # Match both .json (Config) and .jsonl (LinesConfig) backup files.
    files = sorted(glob.glob(os.path.join(instance_dir, f"*.{config_type}")))
    to_delete = files[:-retention]
    for f in to_delete:
        try:
            os.unlink(f)
            log.debug(f"pruned old backup: {f}")
        except OSError as e:
            log.error(f"prune failed for {f}: {e}")

def _make_backup_thread(
    *, # NOTE: kwargs only for safety
    path: str,
    indent: int,
    backup_dir: str,
    backup_interval: int | float,
    backup_retention: int,
    stop_event: threading.Event,
    config_type: CONFIG_TYPES,
    raw: bool = False,
    jsonc: bool = False,
) -> threading.Thread:
    """
    Args:
        path: Path to file.
        indent: Amount of spaces to use as indent.
        backup_dir: Path to backup.
        backup_interval: Time in seconds between backups.
        stop_event: Threading event to use.
        raw: see _do_backup raw kwarg.
        jsonc: see _do_backup jsonc kwarg.
    """
    def log_failure(log: Logger, failures: int, exc: Exception) -> None:
        if failures == 1:
            log.error(f"backup failed for {path}", exc_info=exc)
        elif failures % 3 == 0:
            log.critical(
                f"backup still failing for {path} after {failures} consecutive attempts",
                exc_info=exc,
            )

    def loop() -> None:
        log = Logger("Backup")
        instance_dir = _instance_backup_dir(path, backup_dir)
        failures = 0
        while not stop_event.wait(backup_interval):
            try:
                _do_backup(path, indent, instance_dir, log, raw=raw, jsonc=jsonc)
                _prune_backups(instance_dir, backup_retention, log, config_type=config_type)
                log.info("backup successful")
                failures = 0
                continue
            except Exception as exc:
                failures += 1
                log_failure(log, failures, exc)

            retry_interval = min(backup_interval, 60 * failures)
            if stop_event.wait(retry_interval):
                break
            try:
                _do_backup(path, indent, instance_dir, log, raw=raw, jsonc=jsonc)
                _prune_backups(instance_dir, backup_retention, log, config_type=config_type)
                log.info("backup retry successful")
                failures = 0
                continue
            except Exception as retry_exc:
                failures += 1
                log_failure(log, failures, retry_exc)
    return threading.Thread(target=loop, daemon=True, name="Backup")

