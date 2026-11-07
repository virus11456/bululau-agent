"""ACK 用的最小狀態檔。原子換檔；失敗時記憶體回到上次成功的內容。

每次寫入用新的暫存名。既有的 ack_state.json.tmp 或其他未知檔不刪、不讀。
"""
import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager


class AckStore:
    def __init__(self, directory):
        self.directory = str(directory)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        info = os.lstat(self.directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("rejected_symlink")
        self.path = os.path.join(self.directory, "ack_state.json")
        self.state = {"outbox": {}, "test_events": {}}
        self._fail = False
        self._crash = None
        self._locked = False
        if os.path.lexists(self.path):
            self.reload()

    @contextmanager
    def exclusive(self):
        if self._locked:
            yield
            return
        path = os.path.join(self.directory, "ack_state.lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._locked = True
            try:
                yield
            finally:
                self._locked = False
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def reload(self):
        if not os.path.lexists(self.path):
            self.state = {"outbox": {}, "test_events": {}}
            return self
        info = os.lstat(self.path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("rejected_symlink")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("rejected_state")
        self.state = loaded
        return self

    def save(self):
        if self._locked:
            self._write()
            return
        with self.exclusive():
            self._write()

    def _write(self):
        if self._fail:
            raise OSError("store_failed")
        fd, tmp = self._open_private_tmp()
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            if self._crash == "before_replace":
                raise OSError("crash_before_replace")
            os.replace(tmp, self.path)
            replaced = True
            self._fsync_dir()
            os.chmod(self.path, 0o600)
            if self._crash == "after_replace":
                raise OSError("crash_after_replace")
        except Exception:
            if not replaced and os.path.lexists(tmp) and not stat.S_ISLNK(os.lstat(tmp).st_mode) and self._crash != "before_replace":
                os.unlink(tmp)
            raise

    def _open_private_tmp(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for _ in range(8):
            tmp = "{0}.{1}.{2}.tmp".format(self.path, os.getpid(), secrets.token_hex(8))
            try:
                return os.open(tmp, flags, 0o600), tmp
            except FileExistsError:
                continue
        raise OSError("store_failed")

    def _fsync_dir(self):
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(self.directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
