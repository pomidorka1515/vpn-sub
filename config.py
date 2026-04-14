from collections.abc import MutableMapping, Callable
from loggers import Logger
from typing import ItemsView, KeysView, Self, Any, Iterator, ValuesView
import threading
import os
import json
import jsonschema
import tempfile
try:
    import fcntl
except ModuleNotFoundError:
    raise RuntimeError("Run this on linux.")


class Config(MutableMapping):
    """A JSON config manager.
    Supports file locks for multi worker safety,
    schemas, fully atomic saving.
    platform: only linux, windows wont work.
    To add a schema to your config, use the '$schema' key."""
    def __init__(self, 
                 path: str,
                 indent: int = 4,
                 strict_schema: bool = True):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self._path = path
            self._data = {}
            self._last_mtime = 0
            self._batch_mode = False
            self._lock = threading.RLock()
            self._indent = indent
            self.reload()

            schema_path = self._data.get('$schema')
            if schema_path:
                if schema_path.startswith('http://') or schema_path.startswith('https://'):
                    self.log.warning("JSON schema looks like a link. Skipping.")
                    return
                try:
                    schema_file = os.path.join(os.path.dirname(self._path), schema_path)
                    with open(schema_file) as f:
                        schema = json.load(f)
                    jsonschema.validate(self._data, schema)
                except jsonschema.ValidationError as e:
                    if not strict_schema:
                        self.log.warning(f"Schema validation error: {e.message} -> {list(e.absolute_path)}")
                    else:
                        self.log.critical(f"Schema validation error! Refusing to start.")
                        raise e
                except FileNotFoundError:
                    pass


    def reload(self) -> bool | None:
        """Refresh the data if it was updated outside of Python."""
        try:
            current_mtime = os.path.getmtime(self._path)
            if current_mtime > self._last_mtime:
                with self._lock:
                    with open(self._path, 'r', encoding='utf-8') as f:
                        self._data = json.load(f)
                    self._last_mtime = current_mtime
                return True
            return False
        except FileNotFoundError:
            with self._lock:
                self._data = {}
                self._last_mtime = 0
                self._save_to_disk()
            return True
        except Exception as e:
            raise RuntimeError(f"config file reload error: {e}") from e

    def _atomic_write(self, data) -> None:
        """Atomic write. Nothing much."""
        dir_path = os.path.dirname(self._path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        
        fd, temp_path = tempfile.mkstemp(
            dir=dir_path,
            prefix='.tmp_',
            suffix='.json'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=self._indent, ensure_ascii=False)
            os.replace(temp_path, self._path)
        except Exception:
            os.unlink(temp_path)
            raise

    def _save_to_disk(self) -> None:
        self._atomic_write(self._data)
        self._last_mtime = os.path.getmtime(self._path)

    def _ensure_recent(self) -> None:
        """Ensure the data is up-to-date.
        WARNING: this MUST be called on every method like __getitem__, etc."""
        if self._batch_mode:
            return
        try:
            current_mtime = os.path.getmtime(self._path)
            if current_mtime > self._last_mtime:
                with self._lock:
                    if os.path.getmtime(self._path) > self._last_mtime:
                        with open(self._path, 'r', encoding='utf-8') as f:
                            self._data = json.load(f)
                        self._last_mtime = current_mtime
        except FileNotFoundError:
            pass 
        except json.JSONDecodeError as e:
            self.log.critical(f"""JSON Decoding exception!
            Error: {e}
            Location: position {e.pos}, \nline {e.lineno}, \ncoloumn {e.colno}""")
            return
        except Exception as e:
            self.log.critical(f"EXCEPTION IN CONFIG: {e}")
            return

    def __getitem__(self, key) -> Any:
        with self._lock:
            self._ensure_recent()
            return self._data[key]

    def __setitem__(self, key, value) -> None:
        self.update(lambda d: d.__setitem__(key, value))

    def __delitem__(self, key) -> None:
        def _mut(d):
            if key not in d:
                raise KeyError(key)
            del d[key]
        self.update(_mut)

    def __contains__(self, key) -> bool:
        with self._lock:
            self._ensure_recent()
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            self._ensure_recent()
            return len(self._data)

    def __iter__(self) -> Iterator:
        with self._lock:
            self._ensure_recent()
            return iter(self._data.copy())

    def keys(self) -> KeysView:
        with self._lock:
            self._ensure_recent()
            return self._data.keys()
    
    def values(self) -> ValuesView:
        with self._lock:
            self._ensure_recent()
            return self._data.values()
    
    def items(self) -> ItemsView:
        with self._lock:
            self._ensure_recent()
            return self._data.items()

    # Dictionary access methods
    def get(self, key, default=None) -> Any:
        with self._lock:
            self._ensure_recent()
            return self._data.get(key, default)
    # I know thats not "standard dict bevaviour". 
    def update(self, mutate: Callable[[dict], None] | None = None) -> None: # type: ignore[reportIncompatibleMethodOverride]
        """Main method to mutate the config.
        Everything and anything must go through this!
        Takes a callable with a dict."""
        with self._lock:
            if self._batch_mode:
                # batch mode: caller holds the full scope; re-reading mid-batch
                # would clobber in-progress edits. __exit__ will flush under flock
                if mutate:
                    mutate(self._data)
                return
            lockfile = self._path + '.lock'
            with open(lockfile, 'w') as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    current_mtime = os.path.getmtime(self._path)
                    if current_mtime > self._last_mtime:
                        with open(self._path, 'r', encoding='utf-8') as f:
                            self._data = json.load(f)
                        self._last_mtime = current_mtime
                except FileNotFoundError:
                    pass
                if mutate:
                    mutate(self._data)
                self._save_to_disk()
    _MISSING = object()
    def pop(self, key, default=_MISSING) -> Any:
        box = [] # fugly but works
        def _mut(d):
            if key in d:
                box.append(d.pop(key))
            elif default is not self._MISSING:
                box.append(default)
            else:
                raise KeyError(key)
        self.update(_mut)
        return box[0]

    def popitem(self) -> tuple:
        box = []
        def _mut(d):
            if not d:
                raise KeyError('popitem(): config is empty')
            box.append(d.popitem())
        self.update(_mut)
        return box[0]

    def setdefault(self, key, default=None) -> Any:
        box = []
        def _mut(d):
            if key not in d:
                d[key] = default
            box.append(d[key])
        self.update(_mut)
        return box[0]

    def clear(self) -> None:
        self.update(lambda d: d.clear() if d else None)
    
    def copy(self) -> dict:
        with self._lock:
            self._ensure_recent()
            return self._data.copy()

    def __enter__(self) -> Self:
        """Batch mode support.
        Usage:
        with cfg as d: # Instance of Config, not a regular dict
            d['anything'] += 1
            d['x'].pop("124", None)"""
        self._lock.acquire()
        try:
            self._batch_mode = True
            self._batch_flock_fd = open(self._path + '.lock', 'w')
            fcntl.flock(self._batch_flock_fd, fcntl.LOCK_EX)
            try:
                current_mtime = os.path.getmtime(self._path)
                if current_mtime > self._last_mtime:
                    with open(self._path, 'r', encoding='utf-8') as f:
                        self._data = json.load(f)
                    self._last_mtime = current_mtime
            except FileNotFoundError:
                pass
            return self
        except Exception:
            self._lock.release()
            self._batch_mode = False
            raise
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._batch_mode = False
        try:
            if exc_type is None:
                self._save_to_disk()
        finally:
            if hasattr(self, '_batch_flock_fd') and self._batch_flock_fd:
                try:
                    fcntl.flock(self._batch_flock_fd, fcntl.LOCK_UN)
                    self._batch_flock_fd.close()
                except OSError:
                    pass
                self._batch_flock_fd = None
            self._lock.release()
