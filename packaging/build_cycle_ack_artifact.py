"""在已提交的 commit 上組 cycle ACK 整合套件。不連網，不重打舊 ACK 包。"""
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "src/__init__.py",
    "src/phase1/__init__.py",
    "src/phase1/cycle.py",
    "src/phase1/wake_gate.py",
    "src/phase1/ack_contract.py",
    "src/phase1/ack_store.py",
    "src/phase1/session.py",
    "src/phase1/autoscan.py",
    "src/phase1/token_switch.py",
    "src/phase1/manage_job.py",
    "src/phase1/qty.py",
    "src/phase1/summary.py",
    "tests/__init__.py",
    "tests/test_cycle_ack.py",
    "tests/test_cycle.py",
    "tests/test_wake_gate.py",
    "tests/test_ack_contract.py",
)


def build(out_dir):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    stage = Path(tempfile.mkdtemp())
    bundle = stage / "bululau-cycle-ack"
    for name in FILES:
        dest = bundle / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, dest)
    (bundle / "README.md").write_text((ROOT / "packaging/cycle_ack_bundle_readme.md").read_text(encoding="utf-8"), encoding="utf-8")
    (bundle / "COMMIT").write_text(commit + "\n", encoding="utf-8")
    lines = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append("{0}  {1}".format(digest, path.relative_to(bundle).as_posix()))
    (bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / "bululau-cycle-ack.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle, arcname="bululau-cycle-ack")
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out_dir / "bululau-cycle-ack.tar.gz.sha256").write_text(
        "{0}  bululau-cycle-ack.tar.gz\n".format(archive_sha), encoding="utf-8"
    )
    shutil.rmtree(stage)
    return commit, archive, archive_sha


if __name__ == "__main__":
    dest = os.environ.get("CYCLE_ACK_ARTIFACT_DIR", str(ROOT / "dist"))
    commit, archive, digest = build(dest)
    print(commit)
    print(archive)
    print(digest)
