"""候选预览 -> 确认发布之间的时效守卫与发布登记。

预览到确认之间若出现新校准、曝光回执或关联凭证，任何保证都可能改变：
此时旧候选直接判失效，必须基于新证据重新预览，不允许带病发布。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .isolation import contamination_audit
from .model import Snapshot, canonical_hash

GUARANTEE_TABLES = ("calibrations", "labels", "links", "exposures",
                    "past_manifests", "segments", "coverage")


def candidate_fingerprint(snapshot: Snapshot) -> Dict[str, Any]:
    """候选所依赖的保证指纹：覆盖全部能改变可交付性/隔离/约束的证据。"""
    payload = {}
    for table in GUARANTEE_TABLES:
        payload[table] = [
            {"intake": intake, "record": row}
            for intake, row in snapshot.store.records(table)
            if intake <= snapshot.as_of
        ]
    return {
        "as_of": snapshot.as_of,
        "sha256": canonical_hash(payload),
        "tables": {
            table: canonical_hash(payload[table]) for table in GUARANTEE_TABLES
        },
    }


def staleness_check(store, preview_fingerprint: Dict[str, Any],
                    confirm_as_of: Optional[int] = None) -> Dict[str, Any]:
    """确认时复算指纹并列出改变保证的新证据。"""
    current = store.snapshot(confirm_as_of)
    fresh = candidate_fingerprint(current)
    changed_tables = [
        table for table in GUARANTEE_TABLES
        if preview_fingerprint["tables"].get(table) != fresh["tables"][table]
    ]
    new_intakes = [
        intake for intake in store.intakes
        if intake > preview_fingerprint["as_of"]
    ]
    new_evidence = []
    for intake in new_intakes:
        for delivery in store.deliveries:
            if delivery.intake == intake and delivery.table in GUARANTEE_TABLES:
                new_evidence.append({
                    "intake": intake,
                    "table": delivery.table,
                    "source_file": delivery.source_file,
                    "records": len(delivery.records),
                })
    stale = (
        preview_fingerprint["sha256"] != fresh["sha256"]
        or preview_fingerprint["as_of"] != fresh["as_of"]
    )
    return {
        "stale": stale,
        "preview_as_of": preview_fingerprint["as_of"],
        "confirm_as_of": fresh["as_of"],
        "changed_tables": changed_tables,
        "new_evidence": new_evidence,
        "current_fingerprint": fresh,
        "action": "必须重新预览后再发布" if stale else "保证未变，可以确认发布",
    }


def confirm_release(store, plan: Dict[str, Any],
                    preview_fingerprint: Dict[str, Any],
                    release_name: str) -> Dict[str, Any]:
    """校验候选未过期后登记发布；过期或不可行则拒绝并给出原因。

    时效守卫始终对照台账中最新时点的证据：预览之后到达的任何新校准、
    回执或凭证都会使旧候选失效，即使候选自身的 as_of 没有变。
    """
    candidate = plan.get("candidate") if plan else None
    guard = staleness_check(store, preview_fingerprint)
    if guard["stale"]:
        return {"released": False, "reason": "candidate_stale", "guard": guard}
    if not plan or not plan.get("feasible") or candidate is None:
        return {"released": False, "reason": "candidate_infeasible", "guard": guard}

    snapshot = store.snapshot(candidate["as_of"])
    prior = store.snapshot(max(snapshot.as_of - 1, 0)) if snapshot.as_of > 0 else None
    contamination = (
        contamination_audit(snapshot, prior) if prior is not None else None
    )
    release = {
        "release": release_name,
        "as_of": candidate["as_of"],
        "fingerprint": guard["current_fingerprint"],
        "blind_groups": candidate["blind_groups"],
        "train_groups": candidate["train_groups"],
        "blind_sample_ids": candidate["blind_sample_ids"],
        "train_sample_ids": candidate["train_sample_ids"],
        "scope_counts": candidate["scope_counts"],
        "contamination_snapshot": contamination,
        "rule": (
            "本发布只含 fresh 隔离组；曝光组与污染组按组冻结；"
            "样本编号为内容身份，换编号不能改变来源隔离"
        ),
    }
    release["manifest_sha256"] = canonical_hash(
        {k: v for k, v in release.items() if k != "manifest_sha256"}
    )
    return {"released": True, "release_record": release, "guard": guard}
