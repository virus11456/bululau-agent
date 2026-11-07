"""ACK 用的最小狀態檔。原子換檔；失敗時記憶體回到上次成功的內容。

每次寫入用新的暫存名。既有的 ack_state.json.tmp 或其他未知檔不刪、不讀。
"""
import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager


@contextmanager
def directory_lock(directory, lock_name):
    path = os.path.join(directory, lock_name)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_json_atomic(directory, dest_path, obj, *, fail=False, crash=None):
    """寫入新的暫存檔再換上。不刪、不讀未知的 .tmp。"""
    if fail:
        raise OSError("store_failed")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    tmp = None
    for _ in range(8):
        tmp = "{0}.{1}.{2}.tmp".format(dest_path, os.getpid(), secrets.token_hex(8))
        try:
            fd = os.open(tmp, flags, 0o600)
            break
        except FileExistsError:
            fd = None
    if fd is None:
        raise OSError("store_failed")
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        if crash == "before_replace":
            raise OSError("crash_before_replace")
        os.replace(tmp, dest_path)
        replaced = True
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        dir_fd = os.open(directory, dir_flags)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        os.chmod(dest_path, 0o600)
        if crash == "after_replace":
            raise OSError("crash_after_replace")
    except Exception:
        if not replaced and tmp and os.path.lexists(tmp) and not stat.S_ISLNK(os.lstat(tmp).st_mode) and crash != "before_replace":
            os.unlink(tmp)
        raise


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
        with directory_lock(self.directory, "ack_state.lock"):
            self._locked = True
            try:
                yield
            finally:
                self._locked = False

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
        write_json_atomic(self.directory, self.path, self.state, fail=self._fail, crash=self._crash)
