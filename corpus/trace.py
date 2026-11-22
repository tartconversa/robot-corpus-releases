"""资料员追溯：从一个作业或已发清单成员追到样本可用性、隔离依据与可比范围。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .build import build_lake
from .isolation import (
    contamination_audit,
    episode_to_group,
    exposure_ledger,
    group_locks,
    source_groups,
)
from .model import Snapshot


def trace_episode(store, episode: str, as_of: Optional[int] = None,
                  prior_as_of: Optional[int] = None) -> Dict[str, Any]:
    snapshot = store.snapshot(as_of)
    lake = build_lake(snapshot)
    groups = source_groups(snapshot)
    locks = group_locks(snapshot)
    group_id = episode_to_group(snapshot).get(episode)

    history: List[Dict[str, Any]] = []
    for intake in store.intakes:
        if intake > snapshot.as_of:
            break
        point = store.snapshot(intake)
        point_lake = build_lake(point)
        for sample in point_lake["samples"]:
            if sample["episode"] == episode:
                history.append({
                    "as_of": intake,
                    "status": sample["status"],
                    "sample_id": sample["sample_id"],
                    "blockers": sample["blockers"],
                    "label_id": sample["label"]["id"] if sample["label"] else None,
                    "calibration_id": (
                        sample["calibration"]["id"] if sample["calibration"] else None
                    ),
                })

    result = {
        "episode": episode,
        "as_of": snapshot.as_of,
        "site": next(
            (s["site"] for s in lake["samples"] if s["episode"] == episode), None
        ),
        "current_samples": [
            _sample_brief(s) for s in lake["samples"] if s["episode"] == episode
        ],
        "availability_history": history,
        "isolation": {
            "group_id": group_id,
            "group_episodes": groups.get(group_id, []),
            "lock": locks[group_id]["lock"] if group_id else None,
            "train_members": locks[group_id]["train_members"] if group_id else [],
            "blind_members": locks[group_id]["blind_members"] if group_id else [],
            "link_evidence": _group_evidence(snapshot, groups.get(group_id, [])),
        },
    }
    if prior_as_of is not None and prior_as_of < snapshot.as_of:
        result["contamination_since_prior"] = contamination_audit(
            snapshot, store.snapshot(prior_as_of)
        )
    return result


def trace_member(store, member_id: str, as_of: Optional[int] = None,
                 contamination: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从已发清单成员追当前样本可用性与是否仍为有效盲测。"""
    snapshot = store.snapshot(as_of)
    ledger = exposure_ledger(snapshot)
    if member_id not in ledger:
        raise KeyError(f"清单成员 {member_id} 不存在")
    exposure = ledger[member_id]
    episode = _episode_for_label(snapshot, exposure.label_id)
    episode_trace = trace_episode(store, episode, as_of=as_of)

    contaminated_members = set()
    if contamination is not None:
        contaminated_members.update(contamination["contaminated_blind_members"])
        contaminated_members.update(contamination["train_members_in_same_groups"])

    current_match = None
    for brief in episode_trace["current_samples"]:
        if brief["label_id"] == exposure.label_id:
            current_match = brief
    return {
        "member": member_id,
        "release": exposure.release,
        "role": exposure.role,
        "recipients": list(exposure.recipients),
        "receipt_confirmed": bool(exposure.recipients),
        "episode": episode,
        "released_label_id": exposure.label_id,
        "released_calib_id": exposure.calib_id,
        "current_sample": current_match,
        "still_effective_blind": (
            exposure.role == "blind" and member_id not in contaminated_members
        ),
        "isolation": episode_trace["isolation"],
        "note": (
            "历史成员身份不可改写；若当前样本身份不同，说明动作内容、"
            "边界或口径已变化，旧成员只可按原清单解释"
        ),
    }


def _episode_for_label(snapshot: Snapshot, label_id: str) -> str:
    for _, row in snapshot.store.records("labels"):
        if row["id"] == label_id:
            return row["episode"]
    raise KeyError(f"标签 {label_id} 不存在")


def _sample_brief(sample: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sample_id": sample["sample_id"],
        "status": sample["status"],
        "blockers": sample["blockers"],
        "label_id": sample["label"]["id"] if sample["label"] else None,
        "tag": sample["label"]["tag"] if sample["label"] else None,
        "label_rule": sample["label"]["label_rule"] if sample["label"] else None,
        "cam_ticks": sample["label"]["cam_ticks"] if sample["label"] else None,
        "calibration_id": sample["calibration"]["id"] if sample["calibration"] else None,
        "mapped_ctl_interval": sample["mapped_ctl_interval"],
    }


def _group_evidence(snapshot: Snapshot, episodes: List[str]) -> List[Dict[str, Any]]:
    wanted = set(episodes)
    out = []
    for eff in snapshot.effective_rows("links"):
        link = eff.record
        if link["episode_left"] in wanted and link["episode_right"] in wanted:
            out.append({
                "id": link["id"], "kind": link["kind"],
                "between": [link["episode_left"], link["episode_right"]],
                "arrived_intake": eff.intake,
            })
    return sorted(out, key=lambda r: r["id"])
