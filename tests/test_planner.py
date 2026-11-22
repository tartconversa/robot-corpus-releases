"""候选划分：下限/上限、整组隔离、短缺与卡点报告。"""
import unittest

from corpus.build import build_lake
from corpus.planner import plan_candidates
from tests.test_lake import make_store


class PlannerOnRealDataTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_intake1_infeasible_and_blockers_named(self):
        snapshot = self.store.snapshot(1)
        plan = plan_candidates(snapshot, build_lake(snapshot))
        self.assertFalse(plan["feasible"])
        shortfalls = {item["coverage_id"]: item for item in plan["shortage"]["scope_shortfalls"]}
        # east/grip：fresh 组里没有可交付 grip（A/B 组已训练冻结）
        self.assertGreater(shortfalls["mix-e"]["deficit"], 0)
        blocking = {g["group_id"] for g in shortfalls["mix-e"]["blocking_groups"]}
        self.assertTrue(blocking, "必须点名卡住的来源组")
        # west/mount：D 盲测曝光、C 自身缺口
        self.assertGreater(shortfalls["mix-w"]["deficit"], 0)

    def test_intake2_still_infeasible_mount_but_grip_repairable(self):
        snapshot = self.store.snapshot(2)
        plan = plan_candidates(snapshot, build_lake(snapshot))
        self.assertFalse(plan["feasible"])
        shortfalls = {item["coverage_id"]: item for item in plan["shortage"]["scope_shortfalls"]}
        # E 修复后 west/grip 的 fresh 可交付动作是 E；H 已在盲测冻结。
        # 下限 1 已满足，短缺为 0
        self.assertEqual(shortfalls["mix-wg"]["fresh_available_actions"], 1)
        self.assertEqual(shortfalls["mix-wg"]["deficit"], 0)
        # west/mount：C 已修复且 fresh，下限 1 可满足；真正无法满足的是 east/grip
        self.assertEqual(shortfalls["mix-w"]["deficit"], 0)
        self.assertGreater(shortfalls["mix-e"]["deficit"], 0)
        # 卡点必须点名冻结的 A/B 训练组
        east_blockers = shortfalls["mix-e"]["blocking_groups"]
        blocked_episodes = {ep for g in east_blockers for ep in g["episodes"]}
        self.assertIn("A", blocked_episodes)
        self.assertIn("B", blocked_episodes)

    def test_intake3_fresh_only_h_remains(self):
        snapshot = self.store.snapshot(3)
        plan = plan_candidates(snapshot, build_lake(snapshot))
        self.assertFalse(plan["feasible"])
        locked = {g["group_id"]: g for g in plan["locked_groups"]}
        # A/B/C/D 大组整体隔离；E、F、G 盲测冻结；H 是唯一 fresh
        fresh_episodes = set()
        for info in locked.values():
            self.assertNotEqual(info["lock"], "fresh")
        # H 所在组不在 locked 列表
        locked_episodes = {ep for g in plan["locked_groups"] for ep in g["episodes"]}
        self.assertNotIn("H", locked_episodes)
        shortfalls = {item["coverage_id"]: item for item in plan["shortage"]["scope_shortfalls"]}
        # 时点 3 唯一 fresh 组是 H，west/grip 下限 1 恰好满足
        self.assertEqual(shortfalls["mix-wg"]["fresh_available_actions"], 1)
        self.assertEqual(shortfalls["mix-wg"]["deficit"], 0)
        self.assertGreater(shortfalls["mix-e"]["deficit"], 0)
        self.assertGreater(shortfalls["mix-w"]["deficit"], 0)

    def test_no_group_ever_split_in_any_solution(self):
        # 在所有时点，任何被使用的组都整组归置
        for as_of in (1, 2, 3):
            snapshot = self.store.snapshot(as_of)
            plan = plan_candidates(snapshot, build_lake(snapshot))
            if plan["feasible"] and plan["candidate"]:
                candidate = plan["candidate"]
                overlap = set(candidate["blind_groups"]) & set(candidate["train_groups"])
                self.assertEqual(overlap, set())


class PlannerSyntheticTests(unittest.TestCase):
    """用内存构造的快照验证可行解的整组与上限语义。"""

    def _build_synthetic_store(self):
        from corpus.model import Delivery, Store
        from pathlib import Path
        import tempfile
        store = Store(assets_dir=Path(tempfile.mkdtemp()))
        rows = {
            "segments": [
                {"id": "cam-x", "episode": "X", "channel": "cam",
                 "ticks": [0, 12], "file": "x.csv", "site": "east"},
                {"id": "ctl-x", "episode": "X", "channel": "ctl",
                 "ticks": [100, 124], "file": "x_ctl.csv", "site": "east"},
                {"id": "cam-y", "episode": "Y", "channel": "cam",
                 "ticks": [0, 12], "file": "y.csv", "site": "east"},
                {"id": "ctl-y", "episode": "Y", "channel": "ctl",
                 "ticks": [100, 124], "file": "y_ctl.csv", "site": "east"},
                {"id": "cam-z", "episode": "Z", "channel": "cam",
                 "ticks": [0, 12], "file": "z.csv", "site": "east"},
                {"id": "ctl-z", "episode": "Z", "channel": "ctl",
                 "ticks": [100, 124], "file": "z_ctl.csv", "site": "east"},
            ],
            "calibrations": [
                {"id": f"c{ep.lower()}", "episode": ep,
                 "cam_ticks": [0, 12], "ctl_ticks": [100, 124], "applies": None}
                for ep in "XYZ"
            ],
            "labels": [
                {"id": f"l{ep.lower()}", "episode": ep, "cam_ticks": [1, 5],
                 "tag": "grip", "state": "reviewed", "label_rule": "v1",
                 "applies": None}
                for ep in "XYZ"
            ],
            "coverage": [
                {"id": "cap", "site": "east", "tag": "grip",
                 "minimum": 1, "maximum_total": 2}
            ],
        }
        for table, records in rows.items():
            store.add_delivery(Delivery(
                intake=1, table=table, records=tuple(records),
                source_file=f"syn-{table}.json", file_sha256="syn",
            ))
        return store

    def test_cap_enforced_and_groups_kept_whole(self):
        store = self._build_synthetic_store()
        snapshot = store.snapshot(1)
        plan = plan_candidates(snapshot, build_lake(snapshot))
        self.assertTrue(plan["feasible"])
        candidate = plan["candidate"]
        blind_actions = next(
            s["blind_actions"] for s in candidate["scope_counts"]
            if s["coverage_id"] == "cap"
        )
        self.assertLessEqual(blind_actions, 2)
        self.assertGreaterEqual(blind_actions, 1)
        # 三个独立组，盲测 2 个动作 -> 恰好两个整组进盲测，其余整组进训练
        self.assertEqual(
            len(candidate["blind_groups"]) + len(candidate["train_groups"]), 3
        )
        self.assertEqual(set(candidate["blind_groups"]) & set(candidate["train_groups"]), set())

    def test_deterministic_output(self):
        store = self._build_synthetic_store()
        snapshot = store.snapshot(1)
        first = plan_candidates(snapshot, build_lake(snapshot))
        second = plan_candidates(snapshot, build_lake(snapshot))
        self.assertEqual(first["candidate"]["blind_groups"],
                         second["candidate"]["blind_groups"])


if __name__ == "__main__":
    unittest.main()
