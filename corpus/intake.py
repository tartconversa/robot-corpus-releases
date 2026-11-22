"""从 ``sources/`` 原件登记入湖，校验结构与引用完整性。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .model import Delivery, Store

REQUIRED_TABLE_KEYS = {"intake", "table", "records"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_store(sources_dir: Path, assets_dir: Path | None = None,
               schema: Dict[str, Dict[str, str]] | None = None) -> Tuple[Store, List[str]]:
    """读取全部交付包并做结构、引用与资产校验，返回台账和问题清单。"""
    store = Store(assets_dir=assets_dir)
    problems: List[str] = []
    paths = sorted(sources_dir.glob("*.json"))
    if not paths:
        problems.append(f"{sources_dir} 下没有交付文件")

    seen_packages = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        missing = REQUIRED_TABLE_KEYS - set(payload)
        if missing:
            problems.append(f"{path.name} 缺少顶层键 {sorted(missing)}")
            continue
        table, intake, records = payload["table"], payload["intake"], payload["records"]
        package_key = (intake, table)
        if package_key in seen_packages:
            problems.append(f"{path.name} 与已有交付包撞键 {package_key}")
        seen_packages.add(package_key)
        if schema is not None:
            problems.extend(_check_schema(path.name, table, records, schema))
        store.add_delivery(Delivery(
            intake=intake,
            table=table,
            records=tuple(records),
            source_file=path.name,
            file_sha256=sha256_file(path),
        ))

    if assets_dir is not None:
        problems.extend(_check_assets(store, assets_dir))
    problems.extend(_check_references(store))
    return store, problems


def _check_schema(file_name, table, records, schema) -> List[str]:
    problems: List[str] = []
    if table not in schema:
        return [f"{file_name}: 未知表 {table}"]
    fields = schema[table]
    ids = set()
    for row in records:
        rid = row.get("id")
        if rid in ids:
            problems.append(f"{file_name}: 表内 id 重复 {rid}")
        ids.add(rid)
        if set(row) != set(fields):
            problems.append(
                f"{file_name} 记录 {rid} 字段集合 {sorted(row)} 与声明 {sorted(fields)} 不一致"
            )
        for name, kind in fields.items():
            if name not in row:
                continue
            value = row[name]
            if value is None:
                continue
            if kind == "text" and not isinstance(value, str):
                problems.append(f"{file_name} 记录 {rid}.{name} 应为 text")
            elif kind == "int" and (not isinstance(value, int) or isinstance(value, bool)):
                problems.append(f"{file_name} 记录 {rid}.{name} 应为 int")
            elif kind == "file_ticks":
                if not (isinstance(value, list) and len(value) == 2
                        and all(isinstance(v, int) for v in value)
                        and value[0] < value[1]):
                    problems.append(f"{file_name} 记录 {rid}.{name} 应为递增两点刻度")
    return problems


def _check_assets(store: Store, assets_dir: Path) -> List[str]:
    problems: List[str] = []
    for _, row in store.records("segments"):
        path = assets_dir / row["file"]
        if not path.is_file():
            problems.append(f"片段 {row['id']} 的资产文件缺失: {row['file']}")
            continue
        if path.stat().st_size == 0:
            problems.append(f"片段 {row['id']} 的资产文件为空: {row['file']}")
    return problems


def _check_references(store: Store) -> List[str]:
    """跨表引用完整性（全量，允许引用晚到批次内的记录）。"""
    problems: List[str] = []
    ids_of: Dict[str, set] = {}
    for table in {d.table for d in store.deliveries}:
        ids_of[table] = {row["id"] for _, row in store.records(table)}

    def known(table: str) -> set:
        return ids_of.get(table, set())

    for _, row in store.records("calibrations"):
        if row["applies"] is not None and row["applies"] not in known("calibrations"):
            problems.append(f"校准 {row['id']} applies 未知记录 {row['applies']}")
    for _, row in store.records("labels"):
        if row["applies"] is not None and row["applies"] not in known("labels"):
            problems.append(f"标签 {row['id']} applies 未知记录 {row['applies']}")
    for _, row in store.records("links"):
        for side in ("episode_left", "episode_right"):
            known_episodes = {r["episode"] for _, r in store.records("segments")}
            if row[side] not in known_episodes:
                problems.append(f"凭证 {row['id']} 的 {side}={row[side]} 没有任何片段")
    for _, row in store.records("exposures"):
        if row["member"] not in known("past_manifests"):
            problems.append(f"回执 {row['id']} 指向未知清单成员 {row['member']}")
    for _, row in store.records("scores"):
        if row["member"] not in known("past_manifests"):
            problems.append(f"成绩 {row['id']} 指向未知清单成员 {row['member']}")
        if not (isinstance(row["score"], int) and 0 <= row["score"] <= 100):
            problems.append(f"成绩 {row['id']} 越界: {row['score']}")
    for _, row in store.records("past_manifests"):
        if row["label"] not in known("labels"):
            problems.append(f"清单成员 {row['id']} 引用未知标签 {row['label']}")
        if row["calib"] not in known("calibrations"):
            problems.append(f"清单成员 {row['id']} 引用未知校准 {row['calib']}")
    return problems
