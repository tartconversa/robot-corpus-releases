"""整数刻度区间与校准换算。

约定（见 README「来源约定」）：

* 所有片段边界左闭右开 ``[lo, hi)``；
* 校准锚点之间是连续线性刻度，锚点之外没有对应关系（禁止外推）；
* 同通道片段只在边界邻接（前一段 ``hi`` 等于后一段 ``lo``）时才视为连续，
  断点两侧不得假装连续，因此覆盖以区间并集显式保留所有缺口。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable, List, Sequence, Tuple

Tick = int
Interval = Tuple[Tick, Tick]
FracInterval = Tuple[Fraction, Fraction]


def valid_interval(interval: Sequence[float]) -> bool:
    """左闭右开区间必须严格小于。"""
    return len(interval) == 2 and interval[0] < interval[1]


def merge_abutting(intervals: Iterable[Interval]) -> List[Interval]:
    """合并重叠或邻接的整数区间；中间有断点则保留为两个区间。"""
    ordered = sorted((lo, hi) for lo, hi in intervals if lo < hi)
    merged: List[Interval] = []
    for lo, hi in ordered:
        if not merged or lo > merged[-1][1]:
            merged.append((lo, hi))
        elif hi > merged[-1][1]:
            merged[-1] = (merged[-1][0], hi)
    return merged


def has_gap(coverage: Sequence[Interval], lo: float, hi: float) -> bool:
    """``[lo, hi)`` 是否未被覆盖集合完全包含。"""
    cursor = lo
    for cov_lo, cov_hi in sorted(coverage):
        if cov_hi <= cursor:
            continue
        if cov_lo > cursor:
            return True
        cursor = max(cursor, cov_hi)
        if cursor >= hi:
            return False
    return cursor < hi


class LinearScale:
    """两个 cam 锚点与两个 ctl 锚点定义的一段线性时基。

    校准只在自身两个锚点张成的 cam 区间内有效；落在锚点外的映射请求
    返回 ``None``，由调用方记为「校准失效」而不是外推。
    """

    def __init__(self, cam_ticks: Sequence[Tick], ctl_ticks: Sequence[Tick]):
        if len(cam_ticks) != 2 or len(ctl_ticks) != 2:
            raise ValueError("校准需要 cam、ctl 各两个锚点")
        if cam_ticks[0] >= cam_ticks[1] or ctl_ticks[0] >= ctl_ticks[1]:
            raise ValueError("校准锚点必须严格递增")
        self.cam_lo, self.cam_hi = Tick(cam_ticks[0]), Tick(cam_ticks[1])
        self.ctl_lo, self.ctl_hi = Tick(ctl_ticks[0]), Tick(ctl_ticks[1])
        # ctl = ctl_lo + slope * (cam - cam_lo)
        self.slope = Fraction(self.ctl_hi - self.ctl_lo, self.cam_hi - self.cam_lo)

    def in_anchor_range(self, cam_lo: float, cam_hi: float) -> bool:
        return self.cam_lo <= cam_lo and cam_hi <= self.cam_hi

    def map_interval(self, cam_lo: float, cam_hi: float) -> FracInterval | None:
        """把 cam 上的左闭右开区间映射到 ctl 刻度；越界返回 None。"""
        if not self.in_anchor_range(cam_lo, cam_hi):
            return None
        mapped_lo = self.ctl_lo + self.slope * (Fraction(cam_lo) - self.cam_lo)
        mapped_hi = self.ctl_lo + self.slope * (Fraction(cam_hi) - self.cam_lo)
        return mapped_lo, mapped_hi


def frac_text(value: Fraction) -> str:
    """分数的稳定文本表示，供谱系与哈希使用。"""
    return f"{value.numerator}/{value.denominator}"


def frac_decimal(value: Fraction, places: int = 6) -> str:
    """供离线作业人工核对的十进制近似值。"""
    scaled = value.numerator * 10**places // value.denominator
    whole, frac = divmod(abs(scaled), 10**places)
    sign = "-" if scaled < 0 else ""
    return f"{sign}{whole}.{frac:0{places}d}"
