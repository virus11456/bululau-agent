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
    "src/config.py",
    "src/common.py",
    "src/prefilter.py",
    "src/live_trader.py",
    "src/phase1/__init__.py",
    "src/phase1/cycle.py",
    "src/phase1/runner.py",
    "src/phase1/production_transport.py",
    "src/phase1/status_contract.py",
    "src/phase1/sender_setup.py",
    "src/phase1/wake_gate.py",
    "src/phase1/ack_contract.py",
    "src/phase1/ack_store.py",
    "src/phase1/session.py",
    "src/phase1/autoscan.py",
    "src/phase1/token_switch.py",
    "src/phase1/manage_job.py",
    "src/phase1/qty.py",
    "src/phase1/summary.py",
    "src/phase1/legacy_root.py",
    "src/phase1/legacy_snapshot.py",
    "src/phase1/timeparse.py",
    "src/phase1/account_status.py",
    "src/phase1/box_receiver.py",
    "src/phase1/return_adapter.py",
    "src/phase1/gateway_client.py",
    "src/phase1/canonical.py",
    "src/phase1/market_adapter.py",
    "src/phase1/review_context.py",
    "src/phase1/route_contract.py",
    "config/review_entry.example.json",
    "tests/__init__.py",
    "tests/test_cycle_ack.py",
    "tests/test_runner.py",
    "tests/test_status_contract.py",
    "tests/test_status_json_command.py",
    "tests/test_production_transport.py",
    "docs/production_transport.md",
    "docs/python39_sandbox_diagnosis.md",
    "tests/test_legacy_root.py",
    "tests/test_timeparse.py",
    "tests/test_mac_cycle.py",
    "tests/test_box_loopback.py",
    "tests/test_delivery_binding.py",
    "tests/test_return_adapter.py",
    "tests/test_gateway_client.py",
    "tests/test_operate_chain.py",
    "tests/test_run33_gates.py",
    "tests/test_run34_gaps.py",
    "tests/test_run35_adapter.py",
    "tests/test_run36_contract.py",
    "tests/test_run37_context.py",
    "tests/test_run38_delivery.py",
    "tests/test_run39_contract.py",
    "tests/test_run40_mixed.py",
    "gateway/client.example.json",
    "gateway/package.json",
    "gateway/package-lock.json",
    "gateway/vercel.json",
    "gateway/DEPLOY.md",
    "gateway/VERIFIED_COMMIT",
    "gateway/lib/contract.js",
    "gateway/lib/auth.js",
    "gateway/lib/blob-store.js",
    "gateway/lib/handler.js",
    "gateway/lib/route.js",
    "gateway/api/v1/installations/[installationId]/judgments/[eventId].js",
    "gateway/test/handler.test.js",
    "gateway/test/vercel-config.test.js",
    "docs/mac_box_wiring.md",
    "docs/route_contract.md",
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
    verified = bundle / "gateway" / "VERIFIED_COMMIT"
    if verified.parent.is_dir():
        verified.write_text(commit + "\n", encoding="utf-8")
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
