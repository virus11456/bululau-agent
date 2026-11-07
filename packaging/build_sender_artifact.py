"""在已提交的 commit 上組僅測試 sender 套件。不連網。"""
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(out_dir):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    stage = Path(tempfile.mkdtemp())
    bundle = stage / "bululau-sender-test"
    bundle.mkdir()
    shutil.copy(ROOT / "src/phase1/sender_setup.py", bundle / "sender_setup.py")
    test = (ROOT / "tests/test_sender_setup.py").read_text(encoding="utf-8")
    test = test.replace("from src.phase1.sender_setup import (", "from sender_setup import (")
    (bundle / "sender_test.py").write_text(test, encoding="utf-8")
    (bundle / "README.md").write_text((ROOT / "packaging/sender_bundle_readme.md").read_text(encoding="utf-8"), encoding="utf-8")
    (bundle / "COMMIT").write_text(commit + "\n", encoding="utf-8")
    lines = []
    for path in sorted(bundle.iterdir()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append("{0}  {1}".format(digest, path.name))
    (bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / "bululau-sender-test.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle, arcname="bululau-sender-test")
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out_dir / "bululau-sender-test.tar.gz.sha256").write_text(
        "{0}  bululau-sender-test.tar.gz\n".format(archive_sha), encoding="utf-8"
    )
    shutil.rmtree(stage)
    return commit, archive, archive_sha


if __name__ == "__main__":
    dest = os.environ.get("SENDER_ARTIFACT_DIR", str(ROOT / "dist"))
    commit, archive, digest = build(dest)
    print(commit)
    print(archive)
    print(digest)
