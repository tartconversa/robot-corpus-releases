"""历史基准解释与共同样本比较。"""
import unittest

from corpus.benchmark import benchmark_status, compare_releases
from corpus.isolation import contamination_audit
from tests.test_lake import make_store


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.snapshot = self.store.snapshot(3)
        self.contamination = contamination_audit(
            self.snapshot, self.store.snapshot(2)
        )

    def test_contaminated_members_flagged_but_history_kept(self):
        status = benchmark_status(self.snapshot, self.contamination)
        members = {m["member"]: m for m in status["members"]}
        self.assertFalse(members["previous-d"]["effective_blind"])
        self.assertEqual(members["previous-d"]["status"], "contaminated_blind")
        self.assertFalse(members["next-d"]["effective_blind"])
        # F、G、E 的盲测成员仍然有效
        for member in ("previous-f", "next-f", "previous-g", "next-g", "next-e"):
            self.assertTrue(members[member]["effective_blind"], member)
        # 训练成员身份保留但不作为盲测
        self.assertEqual(members["previous-a"]["status"], "train_member")
        # 历史记录没有被抹掉
        releases = {r["release"]: r for r in status["releases"]}
        self.assertIn("previous-d", releases["bench-r1"]["members"])
        self.assertEqual(releases["bench-r1"]["contaminated_blind"], 1)

    def test_compare_r1_r2_uses_common_samples_only(self):
        result = compare_releases(
            self.snapshot, "bench-r1", "bench-r2", self.contamination
        )
        common_labels = {c["label_id"] for c in result["common_samples"]}
        # D 污染被排除；E 只在 r2 没有对应；F、G 是共同有效样本
        self.assertEqual(common_labels, {"lf", "lg"})
        self.assertEqual(result["common_sample_count"], 2)
        excluded_flat = {
            name
            for item in result["excluded"]
            for name in (item.get("members") or [item.get("member")])
        }
        self.assertIn("previous-d", excluded_flat)
        self.assertIn("next-e", excluded_flat)

    def test_scores_stay_per_sample_per_evaluator(self):
        result = compare_releases(
            self.snapshot, "bench-r1", "bench-r2", self.contamination
        )
        f_sample = next(c for c in result["common_samples"] if c["label_id"] == "lf")
        scores = f_sample["scores_by_evaluator"]
        lab1 = {s["evaluator"]: s["score"] for s in scores["bench-r1"]}
        lab2 = {s["evaluator"]: s["score"] for s in scores["bench-r2"]}
        self.assertEqual(lab1["lab1"], 77)
        self.assertEqual(lab2["lab2"], 69)
        # 输出里没有整包平均分字段
        self.assertNotIn("package_average", result)

    def test_remaining_coverage_shown(self):
        result = compare_releases(
            self.snapshot, "bench-r1", "bench-r2", self.contamination
        )
        remaining = result["remaining_coverage"]
        self.assertIn("next-e", remaining["bench-r2"]["without_common_counterpart"])
        self.assertEqual(remaining["bench-r1"]["without_common_counterpart"], [])

    def test_same_name_does_not_make_package_comparable(self):
        # 没有共同样本的两个发布比较结果为空，不允许整包分数兜底
        result = compare_releases(
            self.snapshot, "train-r1", "bench-r2", self.contamination
        )
        self.assertEqual(result["common_sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
