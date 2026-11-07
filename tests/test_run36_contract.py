"""跨欄 context 與 30 分鐘間隔。用 runner 真產物，不重跑舊套，不發真 HTTP。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from src.phase1.autoscan import freshness
from src.phase1.canonical import canonical_json
from src.phase1.gateway_client import load_gateway_config, publisher_document
from src.phase1.production_transport import build_context, build_envelope, prepare_handoff
from src.phase1.review_context import parent_package_ready
from src.phase1.runner import DEFAULT_SCAN_SECONDS, _load_config, deploy_report, install_schedule, run_configured
from src.phase1.session import Store

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
NOW = "2026-11-07T12:20:00+08:00"
AS_OF = "2026-11-07T12:19:30+08:00"
EVENT = "c-" + "ef" * 16


def _script(path, body):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)


def _judgment(event_id, evaluation_id, digest, decision="keep_original_ai"):
    return {
        "schema_version": 1,
        "event_type": "dry_run_judgment",
        "event_id": event_id,
        "evaluation_id": evaluation_id,
        "envelope_sha256": digest,
        "decision": decision,
        "basis": {"reason": "snapshot_unknown"},
        "completed_at": "2026-11-07T04:22:00Z",
        "orders_placed": False,
        "ai_execution_proven": False,
    }


def _trace(event_id, evaluation_id, digest):
    return {
        "event_id": event_id,
        "evaluation_id": evaluation_id,
        "trace": "parent wrote this trace",
        "envelope_sha256": digest,
    }


def _retarget(context, mutate):
    import hashlib
    envelope = json.loads(json.dumps(context["envelope"]))
    mutate(envelope)
    digest = hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()
    copied = dict(context)
    copied["envelope"] = envelope
    copied["envelope_sha256"] = digest
    return copied, digest


class Run36ContractTest(unittest.TestCase):
    def test_runner_missing_freshness_can_keep_ai_but_contradictions_cannot_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = os.path.join(tmp, "scan.py")
            manage = os.path.join(tmp, "manage.py")
            status = os.path.join(tmp, "status.py")
            _script(scanner, "import json,sys\njson.dump({'call_ai':False,'n_candidates':0,'candidates':[]}, sys.stdout)\n")
            _script(manage, "raise SystemExit(0)\n")
            _script(status, "import json,sys\njson.dump({'status':'unknown'}, sys.stdout)\nsys.exit(2)\n")
            state = os.path.join(tmp, "state")
            decision = run_configured(Store(state), {
                "mode": "shadow",
                "role": "package",
                "timeout_seconds": 10,
                "interval_seconds": 1800,
                "commands": {
                    "scanner": [PYTHON, scanner],
                    "manage": [PYTHON, manage],
                    "status": [PYTHON, status],
                },
            }, now=NOW, scan_id="s36")
            self.assertEqual(decision["opportunity_scan"], "1800")
            self.assertEqual(decision["scan_interval_seconds"], 1800)
            context = build_context(state, decision["event_id"], "s36")
            freshness_row = context["envelope"]["freshness"]
            self.assertEqual(freshness_row, {"valid": False, "status": "missing", "age_seconds": None})
            digest = context["envelope_sha256"]
            judgment = _judgment(decision["event_id"], "s36", digest)
            trace = _trace(decision["event_id"], "s36", digest)
            ready = parent_package_ready(judgment, trace, context)
            self.assertTrue(ready["ready"])
            self.assertFalse(ready["discretion_complete"])
            self.assertFalse(parent_package_ready(_judgment(decision["event_id"], "s36", digest, "no_trade"), trace, context)["ready"])
            self.assertEqual(
                parent_package_ready(_judgment(decision["event_id"], "s36", digest, "no_trade"), trace, context)["reason"],
                "untrustworthy_discretion",
            )
            contradictions = []

            def stale_but_valid(envelope):
                envelope["freshness"] = {"valid": True, "status": "stale", "age_seconds": 1000}

            def fresh_but_negative(envelope):
                envelope["snapshot"]["as_of"] = AS_OF
                envelope["snapshot"]["status"] = "ok"
                envelope["freshness"] = {"valid": True, "status": "fresh", "age_seconds": -1}

            def both_manage_flags(envelope):
                envelope["snapshot"]["manage_error"] = True
                envelope["snapshot"]["manage_complete"] = True

            def empty_candidate(envelope):
                envelope["candidates"] = [{"symbol": "", "setup": "", "bar_time": "", "ladder": []}]

            for change in (stale_but_valid, fresh_but_negative, both_manage_flags, empty_candidate):
                copied, new_digest = _retarget(context, change)
                body = _judgment(decision["event_id"], "s36", new_digest)
                parent_trace = _trace(decision["event_id"], "s36", new_digest)
                verdict = parent_package_ready(body, parent_trace, copied)
                self.assertFalse(verdict["ready"])
                self.assertEqual(verdict["reason"], "rejected_context")
                contradictions.append(verdict["reason"])
            self.assertEqual(len(contradictions), 4)
            judgment_path = os.path.join(tmp, "judgment.json")
            trace_path = os.path.join(tmp, "trace.json")
            context_path = os.path.join(tmp, "context.json")
            copied, new_digest = _retarget(context, empty_candidate)
            with open(judgment_path, "w", encoding="utf-8") as handle:
                json.dump(_judgment(decision["event_id"], "s36", new_digest), handle)
            with open(trace_path, "w", encoding="utf-8") as handle:
                json.dump(_trace(decision["event_id"], "s36", new_digest), handle)
            with open(context_path, "w", encoding="utf-8") as handle:
                json.dump(copied, handle)
            before = Path(trace_path).read_text(encoding="utf-8")
            config = load_gateway_config({
                "enabled": True, "live_send": True,
                "base_url": "https://bululau-return-isolated-8qi96wkvs-virus11456s-projects.vercel.app",
                "installation_id": "inst-local-1", "key_id": "pub-key", "role": "publisher",
                "ledger_path": os.path.join(tmp, "budget.json"),
                "trial": {
                    "approved": True, "dry_run_only": True, "event_id": decision["event_id"], "approved_at": 1000,
                    "new_ids": 1, "max_wake_posts": 2, "max_judgment_puts": 1, "max_gets": 6, "deadline_seconds": 600,
                },
            })
            with self.assertRaises(ValueError) as caught:
                publisher_document(config, decision["event_id"], judgment_path, trace_path, context_path)
            self.assertEqual(str(caught.exception), "rejected_context")
            self.assertEqual(Path(trace_path).read_text(encoding="utf-8"), before)
            fresh = freshness(AS_OF, NOW)
            trusted = build_envelope(
                {"status": "ok", "as_of": AS_OF, "positions": 0, "orders": 0, "manage_error": False, "manage_complete": True, "status_trusted": True},
                {"candidates": []},
                fresh,
                event_id=EVENT, evaluation_id="s36b", sent_at=NOW, reason="not_quiet",
            )
            self.assertEqual(fresh["status"], "fresh")
            self.assertTrue(fresh["valid"])
            import hashlib
            trusted_digest = hashlib.sha256(canonical_json(trusted).encode("utf-8")).hexdigest()
            trusted_context = dict(context)
            trusted_context.update({
                "event_id": EVENT, "evaluation_id": "s36b", "envelope": trusted, "envelope_sha256": trusted_digest,
            })
            self.assertTrue(parent_package_ready(
                _judgment(EVENT, "s36b", trusted_digest, "no_trade"),
                _trace(EVENT, "s36b", trusted_digest),
                trusted_context,
            )["ready"])
            manifest, _side = prepare_handoff(state, decision["event_id"], "s36", os.path.join(tmp, "ctx.json"), 2700)
            self.assertEqual(manifest["scan_interval_seconds"], 2700)
            self.assertFalse(manifest["post_authorized"])

    def test_thirty_minute_floor_reports_the_configured_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            half = os.path.join(tmp, "half.json")
            with open(half, "w", encoding="utf-8") as handle:
                json.dump({"mode": "production", "role": "mac", "interval_seconds": 1800, "python": PYTHON}, handle)
            loaded = _load_config(half)
            self.assertEqual(loaded["interval_seconds"], 1800)
            report = deploy_report(half)
            self.assertEqual(report["scan_interval_seconds"], 1800)
            omitted = os.path.join(tmp, "omit.json")
            with open(omitted, "w", encoding="utf-8") as handle:
                json.dump({"mode": "production", "role": "mac", "python": PYTHON}, handle)
            self.assertEqual(_load_config(omitted)["interval_seconds"], DEFAULT_SCAN_SECONDS)
            fast = os.path.join(tmp, "fast.json")
            with open(fast, "w", encoding="utf-8") as handle:
                json.dump({"mode": "production", "role": "mac", "interval_seconds": 900, "python": PYTHON}, handle)
            with self.assertRaises(ValueError) as caught:
                _load_config(fast)
            self.assertEqual(str(caught.exception), "scan_interval_too_fast")
            dest = os.path.join(tmp, "schedule")
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            installed = install_schedule(dest, state, str(ROOT), PYTHON)
            self.assertEqual(installed["interval_seconds"], 1800)
            plist = Path(dest, "bululau-cycle-shadow.plist").read_text(encoding="utf-8")
            self.assertIn("<key>StartInterval</key><integer>1800</integer>", plist)
            self.assertIn("<key>Disabled</key><true/>", plist)
