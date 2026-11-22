"""同源隔离组、历史曝光冻结与迟到凭证污染审计。"""
import unittest
from pathlib import Path

from corpus.isolation import (
    LOCK_BLIND_EXPOSED,
    LOCK_BOTH,
    LOCK_FRESH,
    LOCK_TRAIN_EXPOSED,
    contamination_audit,
    group_locks,
    source_groups,
    validate_history_references,
)
from tests.test_lake import make_store


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_intake1_groups_and_locks(self):
        snapshot = self.store.snapshot(1)
        groups = source_groups(snapshot)
        group_of = {}
        for root, members in groups.items():
            for episode in members:
                group_of[episode] = tuple(members)
        # A-B 由 link-ab 成组；其余各自独立
        self.assertEqual(group_of["A"], group_of["B"])
        self.assertEqual(set(group_of["A"]), {"A", "B"})
        locks = group_locks(snapshot)
        ab_root = next(r for r, m in groups.items() if "A" in m)
        # A 已训练、B 未曝光 -> 整组训练冻结，B 不能再进盲测
        self.assertEqual(locks[ab_root]["lock"], LOCK_TRAIN_EXPOSED)
        for episode in ("D", "F", "G"):
            root = next(r for r, m in groups.items() if episode in m)
            self.assertEqual(locks[root]["lock"], LOCK_BLIND_EXPOSED)
        for episode in ("C", "E", "H"):
            root = next(r for r, m in groups.items() if episode in m)
            self.assertEqual(locks[root]["lock"], LOCK_FRESH)

    def test_site_alone_never_links_episodes(self):
        # east 的 A/B/F/G 不允许因为同场址被归组
        snapshot = self.store.snapshot(1)
        groups = source_groups(snapshot)
        all_members = [tuple(m) for m in groups.values()]
        self.assertNotIn(("F", "G"), {tuple(sorted(m)) for m in all_members})

    def test_transitive_closure_at_intake3(self):
        snapshot = self.store.snapshot(3)
        groups = source_groups(snapshot)
        big = next(members for members in groups.values() if len(members) > 2)
        # link-ab + link-bd + link-bc 传递闭包
        self.assertEqual(set(big), {"A", "B", "C", "D"})

    def test_late_link_contamination(self):
        audit = contamination_audit(
            self.store.snapshot(3), self.store.snapshot(2)
        )
        self.assertEqual(sorted(audit["new_link_records"]), ["link-bc", "link-bd"])
        # D 的两个盲测成员污染；A 的训练成员在同组被点名
        self.assertEqual(
            set(audit["contaminated_blind_members"]),
            {"previous-d", "next-d"},
        )
        self.assertEqual(
            audit["train_members_in_same_groups"], ["previous-a"]
        )
        self.assertEqual(audit["contaminated_releases"], ["bench-r1", "bench-r2"])
        seen = {item["member"] for item in audit["seen_samples"]}
        self.assertEqual(seen, {"previous-a", "previous-d", "next-d"})

    def test_no_false_contamination_without_new_links(self):
        audit = contamination_audit(
            self.store.snapshot(2), self.store.snapshot(1)
        )
        self.assertEqual(audit["contaminated_blind_members"], [])
        self.assertEqual(audit["contaminated_releases"], [])

    def test_group_both_exposed_locked_whole(self):
        locks = group_locks(self.store.snapshot(3))
        big_root = next(
            root for root, info in locks.items()
            if set(info["episodes"]) == {"A", "B", "C", "D"}
        )
        self.assertEqual(locks[big_root]["lock"], LOCK_BOTH)
        self.assertEqual(locks[big_root]["blind_members"], ["next-d", "previous-d"])
        self.assertEqual(locks[big_root]["train_members"], ["previous-a"])

    def test_history_references_valid(self):
        self.assertEqual(validate_history_references(self.store.snapshot(3)), [])

    def test_exposure_ledger_requires_receipt_target(self):
        # 回执指向的成员必须存在（数据中 next-e 随批次 2 清单到达）
        from corpus.isolation import exposure_ledger
        ledger = exposure_ledger(self.store.snapshot(2))
        self.assertEqual(ledger["next-e"].recipients, ("lab2",))
        # previous-a 只有 train 回执
        self.assertEqual(ledger["previous-a"].recipients, ("team1",))
        # 有成绩但无回执的成员如实标记
        self.assertEqual(ledger["next-d"].recipients, ())


if __name__ == "__main__":
    unittest.main()
