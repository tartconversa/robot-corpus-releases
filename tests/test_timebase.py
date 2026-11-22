"""时基换算、片段拼接与覆盖缺口规则。"""
import unittest
from fractions import Fraction

from corpus.intervals import (
    LinearScale,
    frac_text,
    has_gap,
    merge_abutting,
)


class IntervalTests(unittest.TestCase):
    def test_merge_abutting_keeps_breakpoints(self):
        # 邻接片段拼起来；中间有缺口则保留两段，绝不假装连续
        self.assertEqual(merge_abutting([(0, 6), (6, 12)]), [(0, 12)])
        self.assertEqual(merge_abutting([(100, 114), (114, 124)]), [(100, 124)])
        self.assertEqual(merge_abutting([(0, 4), (6, 12)]), [(0, 4), (6, 12)])
        self.assertEqual(merge_abutting([(6, 12), (0, 4)]), [(0, 4), (6, 12)])

    def test_has_gap_semantics(self):
        coverage = [(0, 6), (6, 12)]
        self.assertFalse(has_gap(coverage, 4, 9))
        self.assertTrue(has_gap([(0, 5)], 4, 9))
        self.assertTrue(has_gap([(0, 4), (6, 12)], 3, 7))
        # 左闭右开：恰好落在边界上不算缺口
        self.assertFalse(has_gap([(0, 6)], 0, 6))


class LinearScaleTests(unittest.TestCase):
    def setUp(self):
        # cam [0,12) <-> ctl [100,124)，斜率 2
        self.scale = LinearScale([0, 12], [100, 124])

    def test_mapping_is_exact_fraction(self):
        self.assertEqual(self.scale.map_interval(4, 9), (Fraction(108), Fraction(118)))
        self.assertEqual(self.scale.map_interval(0, 12), (Fraction(100), Fraction(124)))

    def test_anchor_interior_uses_rational_math(self):
        scale = LinearScale([0, 12], [100, 124])
        lo, hi = scale.map_interval(1, 2)
        self.assertEqual(lo, Fraction(102))
        self.assertEqual(hi, Fraction(104))

    def test_no_extrapolation_outside_anchors(self):
        self.assertIsNone(self.scale.map_interval(-1, 4))
        self.assertIsNone(self.scale.map_interval(10, 13))

    def test_replacement_scale(self):
        # 批次 3 的 C 校准：cam [0,12) <-> ctl [104,128)，斜率仍为 2
        new_scale = LinearScale([0, 12], [104, 128])
        self.assertEqual(new_scale.map_interval(5, 12), (Fraction(114), Fraction(128)))

    def test_fraction_text_stable(self):
        self.assertEqual(frac_text(Fraction(108, 1)), "108/1")
        self.assertEqual(frac_text(Fraction(217, 2)), "217/2")

    def test_bad_anchors_rejected(self):
        with self.assertRaises(ValueError):
            LinearScale([12, 0], [100, 124])
        with self.assertRaises(ValueError):
            LinearScale([0, 12], [124, 100])


if __name__ == "__main__":
    unittest.main()
