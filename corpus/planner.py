"""训练/盲测候选划分。

硬约束：

1. 隔离单位是同源组（含传递闭包），整组连同派生物同进同出，禁止拆组填表；
2. 只有从未对外交付的 ``fresh`` 组才能进入新发布；盲测曝光组不得转训练，
   训练曝光组不得换编号进盲测，两侧都出现过的组整体隔离；
3. 每个 ``(场址, 标签)`` 作用域的盲测动作数同时满足下限 ``minimum``
   与体量上限 ``maximum_total``（按动作计数）；
4. 无法同时达成时输出短缺与卡住的来源组及原因，而不是放宽约束。
"""
from __future__ import annotations

from itertools import product
from typing import Any, Dict, List, Optional, Tuple

from .isolation import (
    LOCK_BLIND_EXPOSED,
    LOCK_BOTH,
    LOCK_FRESH,
    LOCK_TRAIN_EXPOSED,
    group_locks,
)

LOCK_REASON = {
    LOCK_TRAIN_EXPOSED: "组内有成员已交付训练，整组不得进入新盲测",
    LOCK_BLIND_EXPOSED: "组内有成员已交付盲测，曝光动作不得转训练或重复盲测",
    LOCK_BOTH: "迟到凭证使组内同时存在训练与盲测成员，整体隔离",
}


def _deliverable_index(lake: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    index: Dict[str, List[Dict[str, Any]]] = {}
    for sample in lake["samples"]:
        if sample["status"] == "deliverable":
            index.setdefault(sample["episode"], []).append(sample)
    return index


def _all_labeled_index(lake: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """全部带标签动作（含不可交付），用于解释卡点来源。"""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for sample in lake["samples"]:
        if sample["label"] is not None:
            index.setdefault(sample["episode"], []).append(sample)
    return index


def plan_candidates(snapshot, lake: Dict[str, Any]) -> Dict[str, Any]:
    """生成确定性的候选划分；永远返回完整解释，可行与否都可预览。"""
    locks = group_locks(snapshot)
    deliverable = _deliverable_index(lake)
    scopes = [
        (eff.record["site"], eff.record["tag"], eff.record["minimum"],
         eff.record["maximum_total"], eff.record["id"])
        for eff in sorted(snapshot.effective_rows("coverage"), key=lambda e: e.record["id"])
    ]

    fresh_groups = sorted(
        (info for info in locks.values() if info["lock"] == LOCK_FRESH),
        key=lambda info: info["group_id"],
    )
    locked_groups = sorted(
        (info for info in locks.values() if info["lock"] != LOCK_FRESH),
        key=lambda info: info["group_id"],
    )

    def group_scope_counts(info: Dict[str, Any]) -> Dict[Tuple[str, str], int]:
        counts: Dict[Tuple[str, str], int] = {}
        for episode in info["episodes"]:
            for sample in deliverable.get(episode, []):
                key = (sample["site"], sample["label"]["tag"])
                counts[key] = counts.get(key, 0) + 1
        return counts

    group_counts = [(info, group_scope_counts(info)) for info in fresh_groups]
    scope_keys = [(site, tag) for site, tag, _, _, _ in scopes]

    # 穷举 fresh 组的 train/blind 归置（组数量小，且必须保留全部可行解释）
    solutions: List[Tuple[Tuple[str, ...], Dict[Tuple[str, str], int]]] = []
    for assignment in product((ROLE_TRAIN, ROLE_BLIND), repeat=len(group_counts)):
        blind_counts: Dict[Tuple[str, str], int] = {key: 0 for key in scope_keys}
        blind_roots: List[str] = []
        for (info, counts), role in zip(group_counts, assignment):
            if role == ROLE_BLIND:
                blind_roots.append(info["group_id"])
                for key, amount in counts.items():
                    blind_counts[key] = blind_counts.get(key, 0) + amount
        if _satisfies(blind_counts, scopes):
            solutions.append((tuple(sorted(blind_roots)), blind_counts))

    # 选择盲测体量最小的可行方案；再按组 id 字典序稳定裁决
    solutions.sort(key=lambda item: (
        sum(item[1].values()),
        item[0],
    ))

    locked_explanation = [
        {
            "group_id": info["group_id"],
            "episodes": info["episodes"],
            "lock": info["lock"],
            "reason": LOCK_REASON[info["lock"]],
            "train_members": info["train_members"],
            "blind_members": info["blind_members"],
        }
        for info in locked_groups
    ]

    if not solutions:
        return {
            "as_of": snapshot.as_of,
            "feasible": False,
            "candidate": None,
            "shortage": _shortage_report(
                scopes, group_counts, locked_groups, deliverable, lake
            ),
            "locked_groups": locked_explanation,
            "rule": "禁止拆组、禁止复用曝光来源；无法达成即如实交代短缺",
        }

    blind_roots, blind_counts = solutions[0]
    blind_set = set(blind_roots)
    blind_samples, train_samples = [], []
    for info, counts in group_counts:
        target = blind_samples if info["group_id"] in blind_set else train_samples
        for episode in info["episodes"]:
            target.extend(deliverable.get(episode, []))

    candidate = {
        "as_of": snapshot.as_of,
        "feasible": True,
        "blind_groups": sorted(blind_roots),
        "train_groups": sorted(
            info["group_id"] for info, _ in group_counts
            if info["group_id"] not in blind_set
        ),
        "blind_sample_ids": sorted(s["sample_id"] for s in blind_samples),
        "train_sample_ids": sorted(s["sample_id"] for s in train_samples),
        "blind_samples": sorted(
            ({"sample_id": s["sample_id"], "episode": s["episode"],
              "site": s["site"], "tag": s["label"]["tag"]} for s in blind_samples),
            key=lambda r: r["sample_id"],
        ),
        "train_samples": sorted(
            ({"sample_id": s["sample_id"], "episode": s["episode"],
              "site": s["site"], "tag": s["label"]["tag"]} for s in train_samples),
            key=lambda r: r["sample_id"],
        ),
        "scope_counts": [
            {
                "coverage_id": cid,
                "site": site,
                "tag": tag,
                "minimum": minimum,
                "maximum_total": maximum,
                "blind_actions": blind_counts.get((site, tag), 0),
            }
            for site, tag, minimum, maximum, cid in scopes
        ],
        "alternative_count": len(solutions) - 1,
        "evidence_fingerprint": snapshot.evidence(),
    }
    return {
        "as_of": snapshot.as_of,
        "feasible": True,
        "candidate": candidate,
        "shortage": None,
        "locked_groups": locked_explanation,
    }


ROLE_TRAIN = "train"
ROLE_BLIND = "blind"


def _satisfies(blind_counts, scopes) -> bool:
    for site, tag, minimum, maximum, _ in scopes:
        amount = blind_counts.get((site, tag), 0)
        if amount < minimum or amount > maximum:
            return False
    return True


def _shortage_report(scopes, group_counts, locked_groups, deliverable, lake) -> Dict[str, Any]:
    """对每个不达标作用域给出缺口与卡住的来源组。

    卡点有两类，都必须点名：
    * 冻结组里本可贡献该作用域的动作（历史曝光约束）；
    * 当前不可交付的同作用域动作（覆盖缺口、未审标签、校准失效）。
    """
    # 所有 fresh 组都能贡献的理论上限
    fresh_capacity: Dict[Tuple[str, str], int] = {}
    for _, counts in group_counts:
        for key, amount in counts.items():
            fresh_capacity[key] = fresh_capacity.get(key, 0) + amount

    all_labeled = _all_labeled_index(lake)
    reports = []
    for site, tag, minimum, maximum, cid in scopes:
        key = (site, tag)
        available = fresh_capacity.get(key, 0)
        blockers = []
        if available < minimum:
            for info in locked_groups:
                touched = any(
                    s["site"] == site and s["label"]["tag"] == tag
                    for episode in info["episodes"]
                    for s in all_labeled.get(episode, [])
                )
                if touched:
                    blockers.append({
                        "group_id": info["group_id"],
                        "episodes": info["episodes"],
                        "lock": info["lock"],
                        "reason": LOCK_REASON[info["lock"]],
                    })
            # 当前不可交付的动作同样是卡点，按作业列出
            for episode, samples in sorted(all_labeled.items()):
                for sample in samples:
                    if (sample["site"] == site and sample["label"]["tag"] == tag
                            and sample["status"] != "deliverable"):
                        blockers.append({
                            "group_id": None,
                            "episodes": [episode],
                            "lock": sample["status"],
                            "reason": "；".join(sample["blockers"]),
                        })
        reports.append({
            "coverage_id": cid,
            "site": site,
            "tag": tag,
            "minimum": minimum,
            "maximum_total": maximum,
            "fresh_available_actions": available,
            "deficit": max(0, minimum - available),
            "blocking_groups": blockers,
        })

    # 还要检查上限是否在结构上就不可能满足
    cap_violations = [
        {"coverage_id": cid, "site": site, "tag": tag,
         "maximum_total": maximum, "fresh_available_actions": fresh_capacity.get((site, tag), 0)}
        for site, tag, minimum, maximum, cid in scopes
        if fresh_capacity.get((site, tag), 0) > maximum
        and not _can_avoid_excess(group_counts, scopes, (site, tag))
    ]
    return {
        "scope_shortfalls": reports,
        "structural_cap_violations": cap_violations,
    }


def _can_avoid_excess(group_counts, scopes, over_scope) -> bool:
    """是否存在一种整组归置使超限作用域回落到上限内。"""
    for assignment in product((ROLE_TRAIN, ROLE_BLIND), repeat=len(group_counts)):
        counts: Dict[Tuple[str, str], int] = {}
        for (info, scope_amounts), role in zip(group_counts, assignment):
            if role != ROLE_BLIND:
                continue
            for key, amount in scope_amounts.items():
                counts[key] = counts.get(key, 0) + amount
        limits_ok = all(
            counts.get((site, tag), 0) <= maximum
            for site, tag, _, maximum, _ in scopes
        )
        floors_ok = all(
            counts.get((site, tag), 0) >= minimum
            for site, tag, minimum, _, _ in scopes
        )
        if limits_ok and floors_ok:
            return True
    return False
