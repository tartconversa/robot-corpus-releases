"""同源隔离组、历史曝光台账与迟到凭证的污染识别。

隔离规则（见 README「来源约定」）：

* 只服从有编号的 ``links`` 凭证（repeat / mirror / replay），
  关系按传递闭包成组，组内全部样本及派生物必须同进同出；
* 共用机型或班次不构成联系——本数据没有这类字段，也绝不按场址归组；
* 已交给训练方或盲测方的历史集合都构成后续交付约束；
* 已曝光的盲测动作不得转成训练素材；已训练来源不得靠改样本编号进入后续盲测；
* 关联凭证若在两边都交付之后才到，立即标出受污染基准、各方见过的样本，
  之后的发布按新关系隔离；历史发布仍可用原清单解释当时决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from .model import Snapshot

ROLE_TRAIN = "train"
ROLE_BLIND = "blind"

# 组级冻结状态
LOCK_FRESH = "fresh"                    # 从未对外交付
LOCK_TRAIN_EXPOSED = "train_exposed"    # 有成员进入过训练
LOCK_BLIND_EXPOSED = "blind_exposed"    # 有成员进入过盲测
LOCK_BOTH = "both_exposed"              # 凭证迟到：两边都出现过 -> 污染


@dataclass
class UnionFind:
    parent: Dict[str, str] = field(default_factory=dict)

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        self.parent[self.find(right)] = self.find(left)

    def groups(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for item in self.parent:
            out.setdefault(self.find(item), []).append(item)
        return {root: sorted(members) for root, members in out.items()}


def source_groups(snapshot: Snapshot) -> Dict[str, List[str]]:
    """按当前时点生效的凭证计算同源隔离组（含传递关系）。"""
    uf = UnionFind()
    for eff in snapshot.effective_rows("segments"):
        uf.add(eff.record["episode"])
    for eff in snapshot.effective_rows("links"):
        link = eff.record
        if link["kind"] not in {"repeat", "mirror", "replay"}:
            raise ValueError(f"凭证 {link['id']} 的 kind 未知: {link['kind']}")
        uf.union(link["episode_left"], link["episode_right"])
    return uf.groups()


def episode_to_group(snapshot: Snapshot) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for root, members in source_groups(snapshot).items():
        for episode in members:
            mapping[episode] = root
    return mapping


@dataclass(frozen=True)
class Exposure:
    member_id: str
    release: str
    role: str
    label_id: str
    calib_id: str
    recipients: Tuple[str, ...]
    exposure_intake: int  # 回执到达批次；无回执时为 None 兼容字段


def exposure_ledger(snapshot: Snapshot) -> Dict[str, Exposure]:
    """以 past_manifests 成员为键汇总历史交付与实际接收回执。"""
    recipients_by_member: Dict[str, List[str]] = {}
    intake_by_member: Dict[str, int] = {}
    for intake, row in snapshot.store.records("exposures"):
        if intake > snapshot.as_of:
            continue
        recipients_by_member.setdefault(row["member"], []).append(row["recipient"])
        intake_by_member[row["member"]] = min(
            intake, intake_by_member.get(row["member"], intake)
        )

    ledger: Dict[str, Exposure] = {}
    for eff in snapshot.effective_rows("past_manifests"):
        row = eff.record
        if row["role"] not in {ROLE_TRAIN, ROLE_BLIND}:
            raise ValueError(f"历史成员 {row['id']} 的 role 非法: {row['role']}")
        recipients = tuple(sorted(recipients_by_member.get(row["id"], [])))
        ledger[row["id"]] = Exposure(
            member_id=row["id"],
            release=row["release"],
            role=row["role"],
            label_id=row["label"],
            calib_id=row["calib"],
            recipients=recipients,
            exposure_intake=intake_by_member.get(row["id"]),
        )
    # 回执不得指向不存在的清单成员
    for member in recipients_by_member:
        if member not in ledger:
            raise ValueError(f"曝光回执 {member} 在历史清单中不存在")
    return ledger


def group_locks(snapshot: Snapshot) -> Dict[str, Dict[str, Any]]:
    """每个隔离组的冻结状态与证据。"""
    groups = source_groups(snapshot)
    ledger = exposure_ledger(snapshot)

    # 历史成员 -> 作业：历史清单引用的标签可能已被替换，
    # 必须回到全量台账（含已失效版本）定位作业，而不是只看生效标签
    label_episode = {
        row["id"]: row["episode"]
        for _, row in snapshot.store.records("labels")
    }
    member_episode: Dict[str, str] = {}
    for member_id, exposure in ledger.items():
        episode = label_episode.get(exposure.label_id)
        if episode is None:
            raise ValueError(
                f"历史成员 {member_id} 引用的标签 {exposure.label_id} 当前不存在"
            )
        member_episode[member_id] = episode

    info: Dict[str, Dict[str, Any]] = {}
    for root, members in groups.items():
        member_set = set(members)
        train_members = sorted(
            m for m, exp in ledger.items()
            if exp.role == ROLE_TRAIN and member_episode.get(m) in member_set
        )
        blind_members = sorted(
            m for m, exp in ledger.items()
            if exp.role == ROLE_BLIND and member_episode.get(m) in member_set
        )
        has_train = bool(train_members)
        has_blind = bool(blind_members)
        if has_train and has_blind:
            lock = LOCK_BOTH
        elif has_train:
            lock = LOCK_TRAIN_EXPOSED
        elif has_blind:
            lock = LOCK_BLIND_EXPOSED
        else:
            lock = LOCK_FRESH
        info[root] = {
            "group_id": root,
            "episodes": members,
            "lock": lock,
            "train_members": train_members,
            "blind_members": blind_members,
            "recipients": sorted({
                recipient
                for member_id in train_members + blind_members
                for recipient in ledger[member_id].recipients
            }),
        }
    return info


# -- 迟到凭证污染审计 ---------------------------------------------------------

def contamination_audit(
    snapshot: Snapshot,
    prior_snapshot: Snapshot,
) -> Dict[str, Any]:
    """对比凭证到达前后的隔离组，识别被迟到关联污染的历史盲测。

    判定：同一隔离组内同时存在 train 与 blind 历史成员即污染。
    历史发布不被改写，只追加污染标注，原清单仍可解释当时的决定。
    """
    prior_locks = group_locks(prior_snapshot)
    current_locks = group_locks(snapshot)
    current_groups = source_groups(snapshot)
    prior_groups = source_groups(prior_snapshot)
    ledger = exposure_ledger(snapshot)

    contaminated_members: List[str] = []
    contaminated_releases: Set[str] = set()
    seen_samples: Dict[str, Dict[str, Any]] = {}
    for root, info in current_locks.items():
        if info["lock"] != LOCK_BOTH:
            continue
        prior_roots = {
            episode: prior_snapshot_as_episode_group(prior_groups, episode)
            for episode in info["episodes"]
        }
        distinct_prior = sorted({v for v in prior_roots.values() if v is not None})
        if len(distinct_prior) <= 1:
            continue  # 关系在凭证到达前已存在，不是迟到凭证造成的
        for member_id in info["train_members"] + info["blind_members"]:
            exp = ledger[member_id]
            contaminated_members.append(member_id)
            if exp.role == ROLE_BLIND:
                contaminated_releases.add(exp.release)
            seen_samples[member_id] = {
                "member": member_id,
                "release": exp.release,
                "role": exp.role,
                "label_id": exp.label_id,
                "calib_id": exp.calib_id,
                "recipients": list(exp.recipients),
                "episode": _episode_of_label(snapshot, exp.label_id),
            }

    return {
        "as_of": snapshot.as_of,
        "prior_as_of": prior_snapshot.as_of,
        "new_link_records": [
            eff.record["id"]
            for eff in snapshot.effective_rows("links")
            if eff.intake > prior_snapshot.as_of
        ],
        "contaminated_releases": sorted(contaminated_releases),
        "contaminated_blind_members": sorted(
            m for m in contaminated_members if ledger[m].role == ROLE_BLIND
        ),
        "train_members_in_same_groups": sorted(
            m for m in contaminated_members if ledger[m].role == ROLE_TRAIN
        ),
        "seen_samples": [seen_samples[m] for m in sorted(seen_samples)],
        "current_grouping": [
            {"group_id": root, "episodes": members}
            for root, members in sorted(current_groups.items())
        ],
        "rule": "历史发布不重写；重新导出或计算可比成绩时，污染区段不得作为有效盲测",
    }


def prior_snapshot_as_episode_group(prior_groups: Dict[str, List[str]], episode: str):
    for root, members in prior_groups.items():
        if episode in members:
            return root
    return None


def _episode_of_label(snapshot: Snapshot, label_id: str) -> Optional[str]:
    eff = snapshot.row("labels", label_id)
    if eff is not None:
        return eff.record["episode"]
    for _, row in snapshot.store.records("labels"):
        if row["id"] == label_id:
            return row["episode"]
    return None


def validate_history_references(snapshot: Snapshot) -> List[str]:
    """校验历史清单引用的标签/校准在原件中存在（允许已失效版本）。"""
    problems: List[str] = []
    known_labels = {rid for _, row in snapshot.store.records("labels") for rid in [row["id"]]}
    known_calibs = {rid for _, row in snapshot.store.records("calibrations") for rid in [row["id"]]}
    for eff in snapshot.effective_rows("past_manifests"):
        row = eff.record
        if row["label"] not in known_labels:
            problems.append(f"历史成员 {row['id']} 引用未知标签 {row['label']}")
        if row["calib"] not in known_calibs:
            problems.append(f"历史成员 {row['id']} 引用未知校准 {row['calib']}")
    return problems
