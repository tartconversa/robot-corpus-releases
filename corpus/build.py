"""按 as-of 快照构建动作样本、判定可交付性、比较重建差异并导出样本包。

一个动作只有在下列条件全部成立时才可交付（``deliverable``）：

1. 存在生效标签且 ``state == reviewed``（pending 是「未审标签」，
   与「无动作」是两种不同状态）；
2. cam、ctl 两个约定通道对标签的同一物理区段都有可验证覆盖，
   跨文件片段只在边界邻接时拼接，断点保留为缺口；
3. 标签依赖的校准仍有效：整个 cam 区间落在校准锚点之内（锚点外无对应），
   且映射出的 ctl 区间同样被 ctl 覆盖完全包含。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .intervals import (
    LinearScale,
    frac_decimal,
    frac_text,
    has_gap,
    merge_abutting,
)
from .model import Snapshot, canonical_hash

# 状态枚举 -------------------------------------------------------------------

DELIVERABLE = "deliverable"
PENDING_LABEL = "pending_label"      # 有标签但未复核
NO_LABEL = "no_label"                # 没有任何动作标签
NO_CALIBRATION = "no_calibration"    # 缺少有效校准
OUTSIDE_ANCHORS = "outside_anchors"  # 标签落在校准锚点之外
MISSING_CHANNEL = "missing_channel"  # 约定通道覆盖不住该物理区段
LABEL_OUT_OF_CAM = "label_out_of_cam"

# 重建差异类型 ---------------------------------------------------------------

DIFF_SAME = "same"
DIFF_NEW = "new"
DIFF_LOST = "lost"
DIFF_BOUNDARY = "boundary_adjusted"
DIFF_CALIBRATION = "calibration_changed"
DIFF_LABEL_RULE = "label_rule_changed"


@dataclass(frozen=True)
class ChannelCoverage:
    intervals: Tuple[Tuple[int, int], ...]
    segments: Tuple[Dict[str, Any], ...]


def channel_coverage(snapshot: Snapshot, episode: str, channel: str) -> ChannelCoverage:
    """汇总某作业某通道的真实覆盖；不邻接的片段保留断点。"""
    segments = [
        eff.record
        for eff in snapshot.effective_rows("segments")
        if eff.record["episode"] == episode and eff.record["channel"] == channel
    ]
    segments.sort(key=lambda r: (r["ticks"][0], r["id"]))
    intervals = merge_abutting(tuple(r["ticks"]) for r in segments)
    return ChannelCoverage(intervals=tuple(intervals), segments=tuple(segments))


def _label_root(snapshot: Snapshot, label_id: str) -> str:
    chain = snapshot.history("labels", label_id)
    return chain[0].record["id"] if chain else label_id


def _chain_ids(snapshot: Snapshot, table: str, rid: str) -> List[str]:
    return [eff.record["id"] for eff in snapshot.history(table, rid)]


def evaluate_episode(snapshot: Snapshot, episode: str) -> Dict[str, Any]:
    """评估一个作业在当前 as-of 时点的全部动作样本。"""
    cam = channel_coverage(snapshot, episode, "cam")
    ctl = channel_coverage(snapshot, episode, "ctl")
    site = _episode_site(snapshot, episode)

    labels = [
        eff.record
        for eff in snapshot.effective_rows("labels")
        if eff.record["episode"] == episode
    ]
    calibrations = [
        eff.record
        for eff in snapshot.effective_rows("calibrations")
        if eff.record["episode"] == episode
    ]
    if len(calibrations) > 1:
        raise ValueError(f"作业 {episode} 同时存在 {len(calibrations)} 条生效校准")
    calib = calibrations[0] if calibrations else None

    if not labels:
        return [_skeleton(episode, site, cam, ctl, calib, snapshot, label=None)]
    return [
        _evaluate_label(snapshot, episode, site, cam, ctl, calib, label)
        for label in sorted(labels, key=lambda r: r["id"])
    ]


def _episode_site(snapshot: Snapshot, episode: str) -> Optional[str]:
    sites = {
        eff.record["site"]
        for eff in snapshot.effective_rows("segments")
        if eff.record["episode"] == episode
    }
    if len(sites) > 1:
        raise ValueError(f"作业 {episode} 的场址标注不一致: {sorted(sites)}")
    return next(iter(sites), None)


def _skeleton(snapshot, episode, site, cam, ctl, calib, label) -> Dict[str, Any]:
    return {
        "episode": episode,
        "site": site,
        "status": NO_LABEL,
        "blockers": ["该作业在当前时点没有任何动作标签"],
        "label": None,
        "calibration": _calib_info(calib),
        "cam_coverage": _coverage_info(cam),
        "ctl_coverage": _coverage_info(ctl),
        "mapped_ctl_interval": None,
        "sample_id": None,
        "lineage": _lineage(snapshot, episode, label, calib, None),
    }


def _evaluate_label(snapshot, episode, site, cam, ctl, calib, label) -> Dict[str, Any]:
    blockers: List[str] = []
    status = DELIVERABLE
    cam_lo, cam_hi = label["cam_ticks"]

    if label["state"] != "reviewed":
        status = PENDING_LABEL
        blockers.append(f"标签 {label['id']} 状态为 {label['state']}，未复核不得交付")

    if has_gap(cam.intervals, cam_lo, cam_hi):
        status = MISSING_CHANNEL
        blockers.append(
            f"cam 通道对标签区间 [{cam_lo},{cam_hi}) 存在覆盖缺口，"
            f"现有覆盖 {list(cam.intervals)}"
        )

    scale: Optional[LinearScale] = None
    mapped: Optional[Tuple[Fraction, Fraction]] = None
    if calib is None:
        status = NO_CALIBRATION
        blockers.append(f"作业 {episode} 没有有效校准")
    else:
        scale = LinearScale(calib["cam_ticks"], calib["ctl_ticks"])
        mapped = scale.map_interval(cam_lo, cam_hi)
        if mapped is None:
            status = OUTSIDE_ANCHORS
            blockers.append(
                f"标签区间 [{cam_lo},{cam_hi}) 超出校准 {calib['id']} 的锚点 "
                f"{calib['cam_ticks']}，锚点外没有对应关系"
            )
        elif has_gap(ctl.intervals, mapped[0], mapped[1]):
            status = MISSING_CHANNEL
            blockers.append(
                "ctl 通道对映射区间 "
                f"[{frac_text(mapped[0])},{frac_text(mapped[1])}) 存在覆盖缺口，"
                f"现有覆盖 {list(ctl.intervals)}"
            )

    sample = {
        "episode": episode,
        "site": site,
        "status": status,
        "blockers": blockers,
        "label": {
            "id": label["id"],
            "tag": label["tag"],
            "state": label["state"],
            "label_rule": label["label_rule"],
            "cam_ticks": [cam_lo, cam_hi],
            "root_id": _label_root(snapshot, label["id"]),
        },
        "calibration": _calib_info(calib),
        "cam_coverage": _coverage_info(cam),
        "ctl_coverage": _coverage_info(ctl),
        "mapped_ctl_interval": _mapped_info(mapped),
        "sample_id": None,
        "lineage": _lineage(snapshot, episode, label, calib, mapped),
    }
    if status == DELIVERABLE:
        sample["sample_id"] = "smp-" + canonical_hash(_identity_inputs(sample))[:16]
    return sample


def _calib_info(calib: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if calib is None:
        return None
    return {
        "id": calib["id"],
        "cam_ticks": list(calib["cam_ticks"]),
        "ctl_ticks": list(calib["ctl_ticks"]),
    }


def _coverage_info(coverage: ChannelCoverage) -> Dict[str, Any]:
    return {
        "intervals": [list(iv) for iv in coverage.intervals],
        "segments": [
            {
                "id": seg["id"],
                "ticks": list(seg["ticks"]),
                "file": seg["file"],
            }
            for seg in coverage.segments
        ],
    }


def _mapped_info(mapped: Optional[Tuple[Fraction, Fraction]]) -> Optional[Dict[str, str]]:
    if mapped is None:
        return None
    return {
        "lo": frac_text(mapped[0]),
        "hi": frac_text(mapped[1]),
        "lo_decimal": frac_decimal(mapped[0]),
        "hi_decimal": frac_decimal(mapped[1]),
    }


def _lineage(snapshot: Snapshot, episode, label, calib, mapped) -> Dict[str, Any]:
    label_chain = _chain_ids(snapshot, "labels", label["id"]) if label else []
    calib_chain = _chain_ids(snapshot, "calibrations", calib["id"]) if calib else []
    links = [
        eff.record
        for eff in snapshot.effective_rows("links")
        if episode in (eff.record["episode_left"], eff.record["episode_right"])
    ]
    return {
        "built_as_of": snapshot.as_of,
        "label_chain": label_chain,
        "calibration_chain": calib_chain,
        "link_evidence": [
            {"id": link["id"], "kind": link["kind"],
             "episode_left": link["episode_left"], "episode_right": link["episode_right"]}
            for link in sorted(links, key=lambda r: r["id"])
        ],
        "mapped_ctl_interval": _mapped_info(mapped),
    }


def _identity_inputs(sample: Dict[str, Any]) -> Dict[str, Any]:
    """参与样本内容身份的全部输入；改任一项都会得到新样本编号。"""
    return {
        "episode": sample["episode"],
        "label_root": sample["label"]["root_id"],
        "label_id": sample["label"]["id"],
        "cam_ticks": sample["label"]["cam_ticks"],
        "tag": sample["label"]["tag"],
        "label_rule": sample["label"]["label_rule"],
        "calib_id": sample["calibration"]["id"],
        "calib_cam_ticks": sample["calibration"]["cam_ticks"],
        "calib_ctl_ticks": sample["calibration"]["ctl_ticks"],
        "mapped_ctl": sample["mapped_ctl_interval"],
        "cam_segments": [s["id"] for s in sample["cam_coverage"]["segments"]],
        "ctl_segments": [s["id"] for s in sample["ctl_coverage"]["segments"]],
    }


# -- 整湖构建与重建差异 -------------------------------------------------------

def build_lake(snapshot: Snapshot) -> Dict[str, Any]:
    """构建当前时点的全量样本湖。"""
    episodes = sorted(
        {eff.record["episode"] for eff in snapshot.effective_rows("segments")}
    )
    samples: List[Dict[str, Any]] = []
    for episode in episodes:
        samples.extend(evaluate_episode(snapshot, episode))
    deliverable = [s for s in samples if s["status"] == DELIVERABLE]
    return {
        "as_of": snapshot.as_of,
        "evidence": snapshot.evidence(),
        "episodes": episodes,
        "samples": samples,
        "deliverable_sample_ids": sorted(s["sample_id"] for s in deliverable),
        "summary": {
            "episodes": len(episodes),
            "samples": len(samples),
            "deliverable": len(deliverable),
            "blocked": len(samples) - len(deliverable),
        },
    }


def diff_builds(old_lake: Dict[str, Any], new_lake: Dict[str, Any]) -> Dict[str, Any]:
    """比较两次构建，回答「哪些旧样本失去对应、哪些只是边界微调」。

    键为（作业, 标签修订链根 id），同一物理动作的历次修订归并比较。
    """
    old_index = _index_samples(old_lake)
    new_index = _index_samples(new_lake)
    changes: List[Dict[str, Any]] = []
    for key in sorted(set(old_index) | set(new_index)):
        old = old_index.get(key)
        new = new_index.get(key)
        changes.append(_classify_change(key, old, new))
    return {
        "from_as_of": old_lake["as_of"],
        "to_as_of": new_lake["as_of"],
        "changes": changes,
        "lost": [c for c in changes if c["change"] == DIFF_LOST],
        "new": [c for c in changes if c["change"] == DIFF_NEW],
    }


def _index_samples(lake: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for sample in lake["samples"]:
        if sample["label"] is None:
            continue
        key = (sample["episode"], sample["label"]["root_id"])
        if key in index:
            raise ValueError(f"湖内动作键冲突: {key}")
        index[key] = sample
    return index


def _classify_change(key, old, new) -> Dict[str, Any]:
    episode, root = key
    base = {"episode": episode, "label_root": root, "old_sample_id": None,
            "new_sample_id": None, "old_status": None, "new_status": None,
            "change": DIFF_SAME, "detail": ""}
    if old is not None:
        base["old_sample_id"] = old["sample_id"]
        base["old_status"] = old["status"]
    if new is not None:
        base["new_sample_id"] = new["sample_id"]
        base["new_status"] = new["status"]

    old_ok = old is not None and old["status"] == DELIVERABLE
    new_ok = new is not None and new["status"] == DELIVERABLE
    if old_ok and not new_ok:
        base["change"] = DIFF_LOST
        base["detail"] = "；".join(new["blockers"]) if new else "新时点无该动作"
        return base
    if not old_ok and new_ok:
        base["change"] = DIFF_NEW
        base["detail"] = "；".join(old["blockers"]) if old else "旧时点无该动作"
        return base
    if not old_ok and not new_ok:
        base["change"] = DIFF_SAME
        base["detail"] = "两个时点均不可交付"
        return base
    if old["sample_id"] == new["sample_id"]:
        return base
    # 两边都可交付但身份变了：区分口径变更、校准变更与边界微调
    if old["label"]["label_rule"] != new["label"]["label_rule"]:
        base["change"] = DIFF_LABEL_RULE
        base["detail"] = (
            f"标签词表 {old['label']['label_rule']} -> "
            f"{new['label']['label_rule']}，两口径不可互换"
        )
    elif old["calibration"]["id"] != new["calibration"]["id"]:
        base["change"] = DIFF_CALIBRATION
        base["detail"] = (
            f"校准 {old['calibration']['id']} -> {new['calibration']['id']}，"
            "控制段对应关系改变"
        )
    else:
        base["change"] = DIFF_BOUNDARY
        base["detail"] = (
            f"边界由 {old['label']['cam_ticks']} 微调为 {new['label']['cam_ticks']}"
        )
    return base


# -- 样本包导出 ---------------------------------------------------------------

def read_segment_rows(assets_dir, file_name: str) -> List[Dict[str, str]]:
    path = assets_dir / file_name
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["ts"] = int(row["ts"])
    return rows


def verify_assets(snapshot: Snapshot, assets_dir) -> List[str]:
    """核对每个生效片段的 CSV 实际刻度与声明 ticks 完全一致。"""
    problems: List[str] = []
    for eff in snapshot.effective_rows("segments"):
        seg = eff.record
        rows = read_segment_rows(assets_dir, seg["file"])
        actual = [row["ts"] for row in rows]
        lo, hi = seg["ticks"]
        if actual != list(range(lo, hi)):
            problems.append(
                f"片段 {seg['id']}({seg['file']}) 声明 [{lo},{hi})，"
                f"实际刻度 {actual[:3]}...{actual[-3:]}"
            )
        if any(row.get("status") not in {"ok", "warn", "error"} for row in rows):
            problems.append(f"片段 {seg['id']} 出现未知采样状态值")
    return problems


def export_bundle(sample: Dict[str, Any], assets_dir) -> Dict[str, Any]:
    """生成可被离线作业直接读取的样本包：原始采样值 + 定位 + 标注。"""
    if sample["status"] != DELIVERABLE:
        raise ValueError(f"作业 {sample['episode']} 不可交付，禁止导出样本包")
    cam_lo, cam_hi = sample["label"]["cam_ticks"]
    mapped_lo = Fraction(sample["mapped_ctl_interval"]["lo"])
    mapped_hi = Fraction(sample["mapped_ctl_interval"]["hi"])
    # 整数采样点落入左闭右开物理区段的判定
    ctl_lo = _ceil(mapped_lo)
    ctl_hi = _ceil(mapped_hi) - 1  # 最后一个满足 ts < mapped_hi 的整数

    bundle = {
        "sample_id": sample["sample_id"],
        "episode": sample["episode"],
        "site": sample["site"],
        "annotation": {
            "tag": sample["label"]["tag"],
            "label_rule": sample["label"]["label_rule"],
            "label_id": sample["label"]["id"],
            "cam_ticks": [cam_lo, cam_hi],
        },
        "timebase": {
            "calibration_id": sample["calibration"]["id"],
            "cam_anchor_ticks": sample["calibration"]["cam_ticks"],
            "ctl_anchor_ticks": sample["calibration"]["ctl_ticks"],
            "mapped_ctl_interval": sample["mapped_ctl_interval"],
            "ctl_integer_rows": [ctl_lo, ctl_hi + 1],
        },
        "channels": {
            "cam": _slice_channel(sample, assets_dir, "cam", cam_lo, cam_hi),
            "ctl": _slice_channel(sample, assets_dir, "ctl", ctl_lo, ctl_hi + 1),
        },
        "lineage": sample["lineage"],
    }
    return bundle


def _ceil(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def _slice_channel(sample, assets_dir, channel: str, lo: int, hi: int) -> List[Dict[str, Any]]:
    wanted = set(range(lo, hi))
    out: List[Dict[str, Any]] = []
    for seg in sample[f"{channel}_coverage"]["segments"]:
        seg_lo, seg_hi = seg["ticks"]
        if seg_hi <= lo or seg_lo >= hi:
            continue
        for row in read_segment_rows(assets_dir, seg["file"]):
            if row["ts"] in wanted:
                out.append({
                    "segment_id": seg["id"],
                    "ts": row["ts"],
                    "value": row["value"],
                    "status": row["status"],
                })
    out.sort(key=lambda r: (r["ts"], r["segment_id"]))
    found = {row["ts"] for row in out}
    if found != wanted:
        missing = sorted(wanted - found)
        raise ValueError(
            f"样本 {sample['sample_id']} 的 {channel} 通道缺少刻度 {missing}"
        )
    return out
