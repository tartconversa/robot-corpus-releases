"""入湖台账与按到达时点（as-of）解析后的快照。

原始交付不可变地登记进 ``Store``；任何分析都基于某个 as-of 时点解析出的
``Snapshot``。修订记录用 ``applies`` 指向被替换记录，空值代表新增；
被替换记录在替换记录到达的时点起失效，旧时点的构建仍可原样复现。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TABLES = (
    "segments",
    "calibrations",
    "labels",
    "links",
    "past_manifests",
    "exposures",
    "scores",
    "coverage",
)


def canonical_hash(payload: Any) -> str:
    """对任意可 JSON 化对象计算稳定的内容哈希。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Delivery:
    intake: int
    table: str
    records: Tuple[Dict[str, Any], ...]
    source_file: str
    file_sha256: str


@dataclass
class Store:
    """全部交付的不可变登记台账。"""

    deliveries: List[Delivery] = field(default_factory=list)
    assets_dir: Optional[Path] = None

    # -- 登记与读取 -------------------------------------------------------

    def add_delivery(self, delivery: Delivery) -> None:
        if any(d.intake == delivery.intake and d.table == delivery.table for d in self.deliveries):
            raise ValueError(f"批次 {delivery.intake} 的 {delivery.table} 已登记，禁止覆盖")
        self.deliveries.append(delivery)

    @property
    def intakes(self) -> List[int]:
        return sorted({d.intake for d in self.deliveries})

    def records(self, table: str) -> List[Tuple[int, Dict[str, Any]]]:
        """返回 ``(到达批次, 记录)``，按到达顺序。"""
        out: List[Tuple[int, Dict[str, Any]]] = []
        for delivery in self.deliveries:
            if delivery.table == table:
                for record in delivery.records:
                    out.append((delivery.intake, record))
        return out

    def all_records(self, table: str) -> List[Dict[str, Any]]:
        return [record for _, record in self.records(table)]

    def snapshot(self, as_of: Optional[int] = None) -> "Snapshot":
        """解析某时点（含该批次）仍有效的记录。"""
        latest = max(self.intakes) if self.intakes else 0
        if as_of is None:
            as_of = latest
        if as_of < 0:
            raise ValueError("as_of 不能为负")
        return Snapshot(self, as_of)


@dataclass(frozen=True)
class EffectiveRecord:
    record: Dict[str, Any]
    intake: int
    replaced_by: Optional[str] = None  # 替换者的记录 id


@dataclass(frozen=True)
class Snapshot:
    store: Store
    as_of: int

    def _effective(self, table: str) -> Dict[str, EffectiveRecord]:
        """按 applies 链解析生效记录，并校验链的合法性。"""
        arrived = [
            (intake, row)
            for intake, row in self.store.records(table)
            if intake <= self.as_of
        ]
        by_id: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        supersedes: Dict[str, str] = {}  # 新 id -> 被替换 id
        for intake, row in arrived:
            rid = row["id"]
            if rid in by_id:
                raise ValueError(f"{table} 记录 id 重复: {rid}")
            by_id[rid] = (intake, row)
            applies = row.get("applies")
            if applies is not None:
                if applies not in by_id:
                    raise ValueError(
                        f"{table} 记录 {rid} 的 applies={applies} 在其到达前不存在"
                    )
                supersedes[rid] = applies
        # 一条旧记录至多被一条新记录替换
        victims: Dict[str, str] = {}
        for new_id, old_id in supersedes.items():
            if old_id in victims:
                raise ValueError(f"{table} 记录 {old_id} 被多条记录替换")
            victims[old_id] = new_id
        effective: Dict[str, EffectiveRecord] = {}
        for rid, (intake, row) in by_id.items():
            effective[rid] = EffectiveRecord(
                record=row,
                intake=intake,
                replaced_by=victims.get(rid),
            )
        return effective

    def effective_rows(self, table: str) -> List[EffectiveRecord]:
        return [row for row in self._effective(table).values() if row.replaced_by is None]

    def row(self, table: str, rid: str) -> Optional[EffectiveRecord]:
        """按 id 取生效记录；已被替换或未到达返回 None。"""
        entry = self._effective(table).get(rid)
        if entry is None or entry.replaced_by is not None:
            return None
        return entry

    def history(self, table: str, rid: str) -> List[EffectiveRecord]:
        """取一条记录的完整修订链（旧 -> 新），含已失效版本。"""
        chain_map = self._effective(table)
        entry = chain_map.get(rid)
        if entry is None:
            return []
        current = entry
        while True:
            applies = current.record.get("applies")
            if applies is None:
                root = current
                break
            current = chain_map[applies]
        ordered: List[EffectiveRecord] = []
        cursor: Optional[EffectiveRecord] = root
        while cursor is not None:
            ordered.append(cursor)
            cursor = chain_map.get(cursor.replaced_by) if cursor.replaced_by else None
        return ordered

    def evidence(self) -> List[Dict[str, Any]]:
        """本快照依赖的全部原件证据（含已被替换的记录来源）。"""
        return [
            {
                "intake": d.intake,
                "table": d.table,
                "source_file": d.source_file,
                "file_sha256": d.file_sha256,
                "records": len(d.records),
            }
            for d in self.store.deliveries
            if d.intake <= self.as_of
        ]
