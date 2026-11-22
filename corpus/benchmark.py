"""历史基准解释与新旧基准的共同样本比较。

原则：

* 历史发布保持原清单可解释，污染只做追加标注，不回写不抹除；
* 重新导出或计算可比成绩时，被判定污染的区段不是有效盲测；
* 新旧基准只能在「双方还可使用、且动作内容/边界/标注口径一致」的
  共同样本上逐样本比较，禁止按同名发布或相同题数做整包分数比较；
* 盲测成绩由每样本两方分别报送，输出也按样本并列，不做整包平均。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .isolation import exposure_ledger


def benchmark_status(snapshot, contamination: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """逐成员解释历史基准：是否有效盲测、是否有回执、是否被污染。"""
    ledger = exposure_ledger(snapshot)
    scores: Dict[str, List[Dict[str, Any]]] = {}
    for eff in snapshot.effective_rows("scores"):
        row = eff.record
        scores.setdefault(row["member"], []).append(
            {"evaluator": row["evaluator"], "score": row["score"], "id": row["id"]}
        )
    contaminated = set()
    if contamination is not None:
        contaminated.update(contamination["contaminated_blind_members"])

    members = []
    for member_id in sorted(ledger):
        exp = ledger[member_id]
        entry = {
            "member": member_id,
            "release": exp.release,
            "role": exp.role,
            "label_id": exp.label_id,
            "calib_id": exp.calib_id,
            "recipients": list(exp.recipients),
            "receipt_confirmed": bool(exp.recipients),
            "scores": sorted(scores.get(member_id, []), key=lambda r: r["id"]),
        }
        if exp.role == "blind":
            entry["effective_blind"] = member_id not in contaminated
            entry["status"] = (
                "contaminated_blind" if member_id in contaminated else "valid_blind"
            )
        else:
            entry["effective_blind"] = False
            entry["status"] = "train_member"
        members.append(entry)

    releases: Dict[str, Dict[str, Any]] = {}
    for entry in members:
        bucket = releases.setdefault(entry["release"], {
            "release": entry["release"],
            "members": [], "valid_blind": 0, "contaminated_blind": 0,
            "train_members": 0,
        })
        bucket["members"].append(entry["member"])
        if entry["status"] == "valid_blind":
            bucket["valid_blind"] += 1
        elif entry["status"] == "contaminated_blind":
            bucket["contaminated_blind"] += 1
        else:
            bucket["train_members"] += 1
    return {
        "as_of": snapshot.as_of,
        "releases": [releases[name] for name in sorted(releases)],
        "members": members,
        "note": "历史清单不回写；contaminated_blind 不得再作为有效盲测参与计算",
    }


def compare_releases(
    snapshot,
    left_release: str,
    right_release: str,
    contamination: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """逐样本比较两次盲测基准的共同有效部分。"""
    status = benchmark_status(snapshot, contamination)
    members = {entry["member"]: entry for entry in status["members"]}

    def blind_members(release: str) -> List[Dict[str, Any]]:
        return [
            entry for entry in status["members"]
            if entry["release"] == release and entry["role"] == "blind"
        ]

    left = blind_members(left_release)
    right = blind_members(right_release)

    # 共同样本键：同一标签 id（同一动作内容、边界与口径）+ 同一校准
    def key_of(entry: Dict[str, Any]):
        return entry["label_id"], entry["calib_id"]

    right_index = {key_of(entry): entry for entry in right}
    left_index = {key_of(entry): entry for entry in left}
    common: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for entry in left:
        mate = right_index.get(key_of(entry))
        if mate is None:
            excluded.append({
                "member": entry["member"], "release": left_release,
                "reason": "另一基准中不存在同标签同校准的对应成员",
            })
            continue
        if not entry["effective_blind"] or not mate["effective_blind"]:
            excluded.append({
                "members": [entry["member"], mate["member"]],
                "reason": "对应区段已判定污染，不能作为有效盲测比较",
            })
            continue
        common.append({
            "label_id": entry["label_id"],
            "calib_id": entry["calib_id"],
            "members": {
                left_release: entry["member"],
                right_release: mate["member"],
            },
            "recipients": {
                left_release: entry["recipients"],
                right_release: mate["recipients"],
            },
            "scores_by_evaluator": {
                left_release: entry["scores"],
                right_release: mate["scores"],
            },
        })
    # 右基准独有的盲测成员同样不具共同可比性
    for entry in right:
        if key_of(entry) not in left_index:
            excluded.append({
                "member": entry["member"], "release": right_release,
                "reason": "另一基准中不存在同标签同校准的对应成员",
            })

    valid_left = {key_of(e) for e in left if e["effective_blind"]}
    valid_right = {key_of(e) for e in right if e["effective_blind"]}
    remaining = {
        left_release: {
            "valid_blind_members": sorted(e["member"] for e in left if e["effective_blind"]),
            "without_common_counterpart": sorted(
                e["member"] for e in left
                if e["effective_blind"] and key_of(e) not in valid_right
            ),
        },
        right_release: {
            "valid_blind_members": sorted(e["member"] for e in right if e["effective_blind"]),
            "without_common_counterpart": sorted(
                e["member"] for e in right
                if e["effective_blind"] and key_of(e) not in valid_left
            ),
        },
    }
    return {
        "as_of": snapshot.as_of,
        "left_release": left_release,
        "right_release": right_release,
        "comparability_rule": (
            "仅按同标签同校准的共同样本逐样本比较；"
            "污染区段排除；不做整包平均分，题数相同也不直接比包分"
        ),
        "common_sample_count": len(common),
        "common_samples": common,
        "excluded": excluded,
        "remaining_coverage": remaining,
    }
