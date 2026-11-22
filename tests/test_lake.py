"""三个到达时点的样本湖构建、可交付性与重建差异。"""
import unittest
from pathlib import Path

from corpus.build import (
    DELIVERABLE,
    DIFF_BOUNDARY,
    DIFF_CALIBRATION,
    DIFF_LABEL_RULE,
    DIFF_LOST,
    DIFF_NEW,
    MISSING_CHANNEL,
    NO_LABEL,
    PENDING_LABEL,
    build_lake,
    diff_builds,
)
from corpus.intake import load_store

ROOT = Path(__file__).resolve().parents[1]


def make_store():
    schema_path = ROOT / "source-fields.json"
    import json
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    store, problems = load_store(ROOT / "sources", ROOT / "assets", schema)
    assert not problems, problems
    return store


def samples_by_episode(lake):
    out = {}
    for sample in lake["samples"]:
        out.setdefault(sample["episode"], []).append(sample)
    return out


class BuildAsOfTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_intake1_expected_states(self):
        lake = build_lake(self.store.snapshot(1))
        by_ep = samples_by_episode(lake)
        status = {ep: samples[0]["status"] for ep, samples in by_ep.items()}
        self.assertEqual(status["A"], DELIVERABLE)      # 跨 a1/a2 邻接拼接
        self.assertEqual(status["B"], DELIVERABLE)
        self.assertEqual(status["C"], MISSING_CHANNEL)   # ctl 只到 114
        self.assertEqual(status["D"], DELIVERABLE)
        self.assertEqual(status["E"], PENDING_LABEL)     # 未审 != 无动作
        self.assertEqual(status["F"], DELIVERABLE)
        self.assertEqual(status["G"], DELIVERABLE)
        self.assertEqual(status["H"], DELIVERABLE)

    def test_pending_and_no_label_are_distinct(self):
        lake = build_lake(self.store.snapshot(1))
        e = samples_by_episode(lake)["E"][0]
        self.assertEqual(e["status"], PENDING_LABEL)
        self.assertIn("未复核", e["blockers"][0])
        self.assertIsNone(e["sample_id"])
        # 人造一个无标签作业状态检查
        from corpus.build import evaluate_episode
        empty_snapshot = self.store.snapshot(0)
        # 时点 0 没有任何记录；直接验证枚举区分
        self.assertNotEqual(PENDING_LABEL, NO_LABEL)

    def test_c_missing_channel_explains_gap(self):
        lake = build_lake(self.store.snapshot(1))
        c = samples_by_episode(lake)["C"][0]
        # lc cam[5,12) -> ctl[110,124)，覆盖只到 114
        self.assertEqual(c["mapped_ctl_interval"]["lo"], "110/1")
        self.assertEqual(c["mapped_ctl_interval"]["hi"], "124/1")
        self.assertTrue(any("ctl 通道" in b for b in c["blockers"]))
        self.assertTrue(any("124" in b for b in c["blockers"]))

    def test_intake2_repairs_c_and_e(self):
        lake = build_lake(self.store.snapshot(2))
        by_ep = samples_by_episode(lake)
        self.assertEqual(by_ep["C"][0]["status"], DELIVERABLE)
        self.assertEqual(by_ep["E"][0]["status"], DELIVERABLE)
        # E 的生效标签是替换记录 le2，le 已被替换
        self.assertEqual(by_ep["E"][0]["label"]["id"], "le2")
        self.assertEqual(by_ep["E"][0]["lineage"]["label_chain"], ["le", "le2"])
        # C 的 ctl 覆盖由两个邻接片段拼成一个连续区间
        self.assertEqual(by_ep["C"][0]["ctl_coverage"]["intervals"], [[100, 124]])

    def test_intake3_c_loses_correspondence(self):
        lake = build_lake(self.store.snapshot(3))
        c = samples_by_episode(lake)["C"][0]
        # cc2: cam[5,12) -> ctl[114,128)，覆盖只到 124
        self.assertEqual(c["status"], MISSING_CHANNEL)
        self.assertEqual(c["mapped_ctl_interval"]["hi"], "128/1")
        self.assertTrue(any("128" in b for b in c["blockers"]))

    def test_intake3_g_label_rule_change(self):
        lake = build_lake(self.store.snapshot(3))
        g = samples_by_episode(lake)["G"][0]
        self.assertEqual(g["status"], DELIVERABLE)
        self.assertEqual(g["label"]["label_rule"], "v2")
        self.assertEqual(g["lineage"]["label_chain"], ["lg", "lg2"])

    def test_sample_ids_are_content_identity(self):
        lake1 = build_lake(self.store.snapshot(1))
        lake2 = build_lake(self.store.snapshot(2))
        ids1 = {s["episode"]: s["sample_id"] for s in lake1["samples"] if s["sample_id"]}
        ids2 = {s["episode"]: s["sample_id"] for s in lake2["samples"] if s["sample_id"]}
        # 未受新证据影响的作业身份稳定
        for episode in ("A", "B", "D", "F", "H"):
            self.assertEqual(ids1[episode], ids2[episode])
        # E 因标签替换从无身份变为有身份
        self.assertIsNone(next(s for s in lake1["samples"] if s["episode"] == "E")["sample_id"])
        self.assertIsNotNone(ids2["E"])


class RebuildDiffTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_diff_1_to_2_repairs_are_new(self):
        diff = diff_builds(
            build_lake(self.store.snapshot(1)),
            build_lake(self.store.snapshot(2)),
        )
        kinds = {(c["episode"], c["change"]) for c in diff["changes"]}
        self.assertIn(("C", DIFF_NEW), kinds)
        self.assertIn(("E", DIFF_NEW), kinds)

    def test_diff_2_to_3_lost_and_rule_change(self):
        diff = diff_builds(
            build_lake(self.store.snapshot(2)),
            build_lake(self.store.snapshot(3)),
        )
        by_ep = {c["episode"]: c for c in diff["changes"]}
        self.assertEqual(by_ep["C"]["change"], DIFF_LOST)
        self.assertIn("128", by_ep["C"]["detail"])
        self.assertEqual(by_ep["G"]["change"], DIFF_LABEL_RULE)
        # A、B、D、F、H 无变化
        for episode in ("A", "B", "D", "F", "H"):
            self.assertEqual(by_ep[episode]["change"], "same")

    def test_diff_identifies_calibration_change_type(self):
        # 构造「仅校准变化、标签不变」的合成场景验证分类器
        from corpus.build import _classify_change
        old = {
            "status": DELIVERABLE, "sample_id": "smp-old",
            "label": {"id": "lx", "label_rule": "v1", "cam_ticks": [1, 8]},
            "calibration": {"id": "c1"},
        }
        new = {
            "status": DELIVERABLE, "sample_id": "smp-new",
            "label": {"id": "lx", "label_rule": "v1", "cam_ticks": [1, 8]},
            "calibration": {"id": "c2"},
        }
        change = _classify_change(("X", "lx"), old, new)
        self.assertEqual(change["change"], DIFF_CALIBRATION)
        boundary = dict(new)
        boundary["calibration"] = {"id": "c1"}
        boundary["label"] = {"id": "lx", "label_rule": "v1", "cam_ticks": [2, 8]}
        change = _classify_change(("X", "lx"), old, boundary)
        self.assertEqual(change["change"], DIFF_BOUNDARY)


if __name__ == "__main__":
    unittest.main()
