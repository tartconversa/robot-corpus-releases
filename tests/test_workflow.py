"""预览-确认时效守卫、发布登记与样本包导出。"""
import json
import unittest
from fractions import Fraction

from corpus.build import build_lake, export_bundle
from corpus.planner import plan_candidates
from corpus.workflow import (
    candidate_fingerprint,
    confirm_release,
    staleness_check,
)
from tests.test_lake import make_store


class StalenessTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_unchanged_evidence_allows_confirm(self):
        snapshot = self.store.snapshot(2)
        plan = plan_candidates(snapshot, build_lake(snapshot))
        fingerprint = candidate_fingerprint(snapshot)
        guard = staleness_check(self.store, fingerprint, 2)
        self.assertFalse(guard["stale"])

    def test_new_calibration_invalidates_old_candidate(self):
        fingerprint = candidate_fingerprint(self.store.snapshot(2))
        guard = staleness_check(self.store, fingerprint, 3)
        self.assertTrue(guard["stale"])
        self.assertIn("calibrations", guard["changed_tables"])
        self.assertIn("links", guard["changed_tables"])
        self.assertEqual(guard["new_evidence"][0]["intake"], 3)
        # 旧候选不能直接发布
        plan = plan_candidates(self.store.snapshot(2), build_lake(self.store.snapshot(2)))
        outcome = confirm_release(self.store, plan["candidate"], fingerprint, "bad-r3")
        self.assertFalse(outcome["released"])
        self.assertEqual(outcome["reason"], "candidate_stale")

    def test_confirm_records_fingerprinted_manifest(self):
        # 真实数据在各时点均不可行；用合成可行场景验证发布登记路径
        from tests.test_planner import PlannerSyntheticTests
        synth = PlannerSyntheticTests()._build_synthetic_store()
        snap = synth.snapshot(1)
        synth_plan = plan_candidates(snap, build_lake(snap))
        fp = candidate_fingerprint(snap)
        outcome = confirm_release(synth, synth_plan, fp, "r-syn")
        self.assertTrue(outcome["released"])
        record = outcome["release_record"]
        self.assertEqual(record["release"], "r-syn")
        self.assertIn("manifest_sha256", record)
        self.assertIsInstance(record["blind_sample_ids"], list)
        self.assertTrue(all(sid.startswith("smp-") for sid in record["blind_sample_ids"]))
        # 清单自证哈希可复算
        payload = {k: v for k, v in record.items() if k != "manifest_sha256"}
        from corpus.model import canonical_hash
        self.assertEqual(record["manifest_sha256"], canonical_hash(payload))


class BundleExportTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_export_carries_raw_values_position_and_annotation(self):
        lake = build_lake(self.store.snapshot(2))
        sample = next(s for s in lake["samples"] if s["episode"] == "A")
        bundle = export_bundle(sample, self.store.assets_dir)
        self.assertEqual(bundle["sample_id"], sample["sample_id"])
        cam_rows = bundle["channels"]["cam"]
        # cam [4,9) 五个整数采样点，跨 a1(4,5) 与 a2(6,7,8) 两个文件
        self.assertEqual([r["ts"] for r in cam_rows], [4, 5, 6, 7, 8])
        self.assertEqual({r["segment_id"] for r in cam_rows}, {"cam-a1", "cam-a2"})
        self.assertTrue(all({"ts", "value", "status", "segment_id"} <= set(r)
                            for r in cam_rows))
        # ctl 映射 [108,118)
        ctl_rows = bundle["channels"]["ctl"]
        self.assertEqual([r["ts"] for r in ctl_rows], list(range(108, 118)))
        self.assertEqual(bundle["annotation"]["cam_ticks"], [4, 9])
        self.assertEqual(bundle["timebase"]["calibration_id"], "ca")
        self.assertEqual(bundle["lineage"]["label_chain"], ["la"])

    def test_export_refuses_blocked_sample(self):
        lake = build_lake(self.store.snapshot(1))
        blocked = next(s for s in lake["samples"] if s["episode"] == "C")
        with self.assertRaises(ValueError):
            export_bundle(blocked, self.store.assets_dir)

    def test_bundle_values_match_source_csv(self):
        import csv
        lake = build_lake(self.store.snapshot(2))
        sample = next(s for s in lake["samples"] if s["episode"] == "H")
        bundle = export_bundle(sample, self.store.assets_dir)
        with (self.store.assets_dir / "h.csv").open(encoding="utf-8", newline="") as handle:
            source = {int(r["ts"]): r["value"] for r in csv.DictReader(handle)}
        for row in bundle["channels"]["cam"]:
            self.assertEqual(row["value"], source[row["ts"]])


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_episode_trace_covers_full_history(self):
        from corpus.trace import trace_episode
        result = trace_episode(self.store, "C", as_of=3, prior_as_of=2)
        statuses = [(h["as_of"], h["status"]) for h in result["availability_history"]]
        self.assertEqual(statuses, [
            (1, "missing_channel"),
            (2, "deliverable"),
            (3, "missing_channel"),
        ])
        self.assertEqual(result["isolation"]["group_episodes"], ["A", "B", "C", "D"])
        self.assertIn("contamination_since_prior", result)
        audit = result["contamination_since_prior"]
        self.assertIn("next-d", audit["contaminated_blind_members"])

    def test_member_trace(self):
        from corpus.trace import trace_member
        from corpus.isolation import contamination_audit
        audit = contamination_audit(self.store.snapshot(3), self.store.snapshot(2))
        result = trace_member(self.store, "previous-d", as_of=3, contamination=audit)
        self.assertFalse(result["still_effective_blind"])
        self.assertEqual(result["recipients"], ["lab1"])
        result_f = trace_member(self.store, "previous-f", as_of=3, contamination=audit)
        self.assertTrue(result_f["still_effective_blind"])


if __name__ == "__main__":
    unittest.main()
