"""ACK 用的最小狀態檔。原子換檔；失敗時記憶體回到上次成功的內容。"""
import json
import os
import stat


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
        if os.path.exists(self.path) and not stat.S_ISLNK(os.lstat(self.path).st_mode):
            with open(self.path, encoding="utf-8") as handle:
                self.state = json.load(handle)

    def save(self):
        if self._fail:
            raise OSError("store_failed")
        tmp = self.path + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.lexists(tmp) and not stat.S_ISLNK(os.lstat(tmp).st_mode):
                os.unlink(tmp)
            raise
        os.chmod(self.path, 0o600)

    def reload(self):
        fresh = AckStore(self.directory)
        self.state = fresh.state
        return self
