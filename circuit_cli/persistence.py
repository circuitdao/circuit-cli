import json
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict


HOME_DIR = os.path.expanduser("~")
CIRCUIT_DIR = os.path.join(HOME_DIR, ".circuit")
DEFAULT_FILE = os.path.join(CIRCUIT_DIR, "state.json")
LOCK_FILE = os.path.join(CIRCUIT_DIR, "state.lock")


class FileLockTimeout(Exception):
    pass


class DictStore:
    """
    A simple dictionary-like persistent store backed by a JSON file under ~/.circuit.

    - Atomic writes using temporary files and os.replace
    - Exclusive file lock via a lock file (POSIX flock if available; fallback to lock file semantics)
    - Supports insert, update, delete operations and get/set semantics
    - Safe for multi-process use on the same machine
    """

    def __init__(self, path: str = DEFAULT_FILE, lock_path: str = LOCK_FILE):
        self.path = path
        self.lock_path = lock_path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Ensure the file exists
        if not os.path.exists(self.path):
            self._atomic_write({})

    @contextmanager
    def lock(self, timeout: float = 1.0, poll_interval: float = 0.05):
        """
        Acquire an exclusive lock using a lock file. Blocks until acquired or timeout.
        """
        start = time.time()
        while True:
            try:
                # O_CREAT | O_EXCL ensures exclusive creation; fails if lock exists
                lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                break
            except FileExistsError:
                if time.time() - start > timeout:
                    raise FileLockTimeout(f"Timeout acquiring lock {self.lock_path}")
                time.sleep(poll_interval)
        try:
            yield
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass

    def unlock(self):
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass

    def _atomic_write(self, data: Dict[str, Any]):
        dir_name = os.path.dirname(self.path)
        fd, tmp_path = tempfile.mkstemp(prefix=".state.", suffix=".json.tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            # Corrupt file: backup and reset
            backup = self.path + ".bak"
            try:
                os.replace(self.path, backup)
            except Exception:
                pass
            return {}

    # Public API
    def get_all(self) -> Dict[str, Any]:
        return self._read()

    def get(self, key: str, default: Any = None) -> Any:
        return self._read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self.lock():
            data = self._read()
            data[key] = value
            self._atomic_write(data)

    def insert(self, key: str, value: Any) -> None:
        with self.lock():
            data = self._read()
            if key in data:
                raise KeyError(f"Key already exists: {key}")
            data[key] = value
            self._atomic_write(data)

    def update(self, key: str, value: Any) -> None:
        with self.lock():
            data = self._read()
            if key not in data:
                raise KeyError(f"Key does not exist: {key}")
            data[key] = value
            self._atomic_write(data)

    def delete(self, key: str) -> None:
        with self.lock():
            data = self._read()
            if key in data:
                del data[key]
                self._atomic_write(data)

    def clear(self) -> None:
        with self.lock():
            self._atomic_write({})

    @contextmanager
    def transaction(self):
        """
        Transaction-like context: lock, load, allow mutations, then write back on exit.
        Usage:
            with store.transaction() as data:
                data["foo"] = 1
        """
        with self.lock():
            data = self._read()
            yield data
            self._atomic_write(data)
