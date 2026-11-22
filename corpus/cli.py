"""语料入湖与发布命令行。

所有子命令只读 ``sources/`` 与 ``assets/`` 原件，产物写到独立工作目录，
便于资料员在同一套项目里接收多次交付、预览、确认与核对。

运行约定：在仓库根目录执行，例如::

    python -I -m corpus.cli ingest --workdir out
    python -I -m corpus.cli build --workdir out --as-of 2
    python -I -m corpus.cli preview --workdir out
    python -I -m corpus.cli confirm --workdir out --release r3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):  # 允许 `python -I corpus/cli.py ...` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from corpus.benchmark import benchmark_status, compare_releases
    from corpus.build import build_lake, diff_builds, export_bundle, verify_assets
    from corpus.intake import load_store
    from corpus.isolation import contamination_audit, group_locks, source_groups
    from corpus.model import canonical_hash
    from corpus.planner import plan_candidates
    from corpus.trace import trace_episode, trace_member
    from corpus.workflow import candidate_fingerprint, confirm_release, staleness_check
else:
    from .benchmark import benchmark_status, compare_releases
    from .build import build_lake, diff_builds, export_bundle, verify_assets
    from .intake import load_store
    from .isolation import contamination_audit, group_locks, source_groups
    from .model import canonical_hash
    from .planner import plan_candidates
    from .trace import trace_episode, trace_member
    from .workflow import candidate_fingerprint, confirm_release, staleness_check

ROOT = Path(__file__).resolve().parents[1]

STORE_FILE = "store.json"
LAKE_TEMPLATE = "lake-asof-{as_of}.json"
DIFF_TEMPLATE = "diff-{old}-{new}.json"
PREVIEW_FILE = "candidate-preview.json"
FINGERPRINT_FILE = "candidate-fingerprint.json"
RELEASE_TEMPLATE = "release-{name}.json"
AUDIT_TEMPLATE = "contamination-{old}-{new}.json"
BENCHMARK_FILE = "benchmark-status.json"
BUNDLE_TEMPLATE = "bundle-{sample_id}.json"


# -- 工作目录持久化 -----------------------------------------------------------

def _workdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def open_store(workdir: Path):
    """从工作目录重建内存台账（store.json 保存的是原件登记与哈希）。"""
    from corpus.model import Delivery, Store
    record = read_json(workdir / STORE_FILE)
    store = Store(assets_dir=Path(record["assets_dir"]))
    for item in record["deliveries"]:
        store.add_delivery(Delivery(
            intake=item["intake"], table=item["table"],
            records=tuple(item["records"]),
            source_file=item["source_file"],
            file_sha256=item["file_sha256"],
        ))
    return store


def _latest_intake(store) -> int:
    return max(store.intakes)


# -- 子命令 -------------------------------------------------------------------

def cmd_ingest(args) -> int:
    sources = Path(args.sources)
    assets = Path(args.assets)
    schema = read_json(Path(args.schema))
    store, problems = load_store(sources, assets, schema)
    workdir = _workdir(Path(args.workdir))
    record = {
        "assets_dir": str(assets),
        "deliveries": [
            {
                "intake": d.intake, "table": d.table,
                "records": list(d.records),
                "source_file": d.source_file, "file_sha256": d.file_sha256,
            }
            for d in store.deliveries
        ],
    }
    write_json(workdir / STORE_FILE, record)

    snapshot = store.snapshot()
    asset_problems = verify_assets(snapshot, assets)
    problems.extend(asset_problems)
    report = {
        "intakes": store.intakes,
        "tables": sorted({d.table for d in store.deliveries}),
        "delivery_counts": [
            {"intake": d.intake, "table": d.table, "records": len(d.records)}
            for d in sorted(store.deliveries, key=lambda x: (x.intake, x.table))
        ],
        "validation_problems": problems,
    }
    write_json(workdir / "intake-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


def cmd_build(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    as_of = args.as_of if args.as_of is not None else _latest_intake(store)
    snapshot = store.snapshot(as_of)
    lake = build_lake(snapshot)
    out = workdir / LAKE_TEMPLATE.format(as_of=as_of)
    write_json(out, lake)
    print(f"as_of={as_of} 样本湖写入 {out}")
    print(json.dumps(lake["summary"], ensure_ascii=False))
    for sample in lake["samples"]:
        flag = "OK " if sample["status"] == "deliverable" else "XX "
        label = sample["label"]["id"] if sample["label"] else "-"
        print(f"  {flag}{sample['episode']} {label} {sample['status']}")
        for blocker in sample["blockers"]:
            print(f"      └ {blocker}")
    return 0


def cmd_rebuild_diff(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    old_lake = build_lake(store.snapshot(args.old))
    new_lake = build_lake(store.snapshot(args.new))
    result = diff_builds(old_lake, new_lake)
    out = workdir / DIFF_TEMPLATE.format(old=args.old, new=args.new)
    write_json(out, result)
    print(f"重建差异写入 {out}")
    for change in result["changes"]:
        print(f"  {change['episode']} {change['label_root']}: {change['change']} {change['detail']}")
    return 0


def cmd_preview(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    as_of = args.as_of if args.as_of is not None else _latest_intake(store)
    snapshot = store.snapshot(as_of)
    lake = build_lake(snapshot)
    plan = plan_candidates(snapshot, lake)
    fingerprint = candidate_fingerprint(snapshot)
    write_json(workdir / PREVIEW_FILE, plan)
    write_json(workdir / FINGERPRINT_FILE, fingerprint)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print(f"候选保证指纹已保存：{workdir / FINGERPRINT_FILE}")
    return 0 if plan["feasible"] else 2


def cmd_confirm(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    plan = read_json(workdir / PREVIEW_FILE)
    fingerprint = read_json(workdir / FINGERPRINT_FILE)
    outcome = confirm_release(store, plan, fingerprint, args.release)
    if not outcome["released"]:
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 3
    out = workdir / RELEASE_TEMPLATE.format(name=args.release)
    write_json(out, outcome["release_record"])
    print(f"发布已登记：{out}")
    print(json.dumps(outcome["guard"], ensure_ascii=False, indent=2))
    return 0


def cmd_stale(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    fingerprint = read_json(workdir / FINGERPRINT_FILE)
    result = staleness_check(store, fingerprint, args.as_of)
    write_json(workdir / "staleness.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["stale"] else 4


def cmd_audit(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    new = args.new if args.new is not None else _latest_intake(store)
    old = args.old if args.old is not None else new - 1
    result = contamination_audit(store.snapshot(new), store.snapshot(old))
    out = workdir / AUDIT_TEMPLATE.format(old=old, new=new)
    write_json(out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_benchmark(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    as_of = args.as_of if args.as_of is not None else _latest_intake(store)
    snapshot = store.snapshot(as_of)
    prior = store.snapshot(max(as_of - 1, 0)) if as_of > 0 else None
    contamination = (
        contamination_audit(snapshot, prior) if prior is not None else None
    )
    status = benchmark_status(snapshot, contamination)
    write_json(workdir / BENCHMARK_FILE, status)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def cmd_compare(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    as_of = args.as_of if args.as_of is not None else _latest_intake(store)
    snapshot = store.snapshot(as_of)
    prior = store.snapshot(max(as_of - 1, 0)) if as_of > 0 else None
    contamination = (
        contamination_audit(snapshot, prior) if prior is not None else None
    )
    result = compare_releases(snapshot, args.left, args.right, contamination)
    out = workdir / f"compare-{args.left}-vs-{args.right}.json"
    write_json(out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_trace(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    if args.episode:
        result = trace_episode(
            store, args.episode, as_of=args.as_of, prior_as_of=args.prior
        )
    else:
        as_of = args.as_of if args.as_of is not None else _latest_intake(store)
        snapshot = store.snapshot(as_of)
        prior = store.snapshot(max(as_of - 1, 0)) if as_of > 0 else None
        contamination = (
            contamination_audit(snapshot, prior) if prior is not None else None
        )
        result = trace_member(store, args.member, as_of=as_of,
                              contamination=contamination)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    as_of = args.as_of if args.as_of is not None else _latest_intake(store)
    lake = build_lake(store.snapshot(as_of))
    assets = Path(store.assets_dir)
    wanted = set(args.sample) if args.sample else set(lake["deliverable_sample_ids"])
    by_id = {s["sample_id"]: s for s in lake["samples"] if s["sample_id"]}
    missing = sorted(wanted - set(by_id))
    if missing:
        print(f"样本不存在或当前不可交付: {missing}")
        return 5
    bundles = []
    for sample_id in sorted(wanted):
        bundle = export_bundle(by_id[sample_id], assets)
        out = workdir / "bundles" / BUNDLE_TEMPLATE.format(sample_id=sample_id)
        write_json(out, bundle)
        bundles.append(str(out))
    index = {
        "as_of": as_of,
        "bundle_count": len(bundles),
        "bundles": bundles,
        "lake_sha256": canonical_hash(lake),
    }
    write_json(workdir / "bundles" / "index.json", index)
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


def cmd_groups(args) -> int:
    workdir = Path(args.workdir)
    store = open_store(workdir)
    as_of = args.as_of if args.as_of is not None else _latest_intake(store)
    snapshot = store.snapshot(as_of)
    result = {
        "as_of": as_of,
        "groups": [
            {"group_id": root, "episodes": members, **group_locks(snapshot)[root]}
            for root, members in sorted(source_groups(snapshot).items())
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corpus.cli",
        description="机器人实操语料入湖、谱系、隔离与发布",
    )
    parser.add_argument("--workdir", default="out", help="产物工作目录（默认 out）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="登记全部源交付并校验")
    p.add_argument("--sources", default=str(ROOT / "sources"))
    p.add_argument("--assets", default=str(ROOT / "assets"))
    p.add_argument("--schema", default=str(ROOT / "source-fields.json"))
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("build", help="按 as-of 时点构建样本湖")
    p.add_argument("--as-of", type=int, default=None)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("rebuild-diff", help="比较两个时点的重建结果")
    p.add_argument("--old", type=int, required=True)
    p.add_argument("--new", type=int, required=True)
    p.set_defaults(func=cmd_rebuild_diff)

    p = sub.add_parser("preview", help="生成训练/盲测候选划分")
    p.add_argument("--as-of", type=int, default=None)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("confirm", help="确认候选并登记发布")
    p.add_argument("--release", required=True)
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("stale", help="检查预览候选是否已被新证据改变保证")
    p.add_argument("--as-of", type=int, default=None)
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("audit", help="迟到凭证污染审计")
    p.add_argument("--old", type=int, default=None)
    p.add_argument("--new", type=int, default=None)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("benchmark", help="历史基准有效性与回执/成绩核对")
    p.add_argument("--as-of", type=int, default=None)
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("compare", help="两次盲测基准的共同样本比较")
    p.add_argument("--left", required=True)
    p.add_argument("--right", required=True)
    p.add_argument("--as-of", type=int, default=None)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("trace", help="从作业或已发成员追溯")
    p.add_argument("--episode", default=None)
    p.add_argument("--member", default=None)
    p.add_argument("--as-of", type=int, default=None)
    p.add_argument("--prior", type=int, default=None)
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("export", help="导出可离线读取的样本包")
    p.add_argument("--sample", action="append", help="sample_id，可重复；默认全部可交付")
    p.add_argument("--as-of", type=int, default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("groups", help="查看当前同源隔离组与冻结状态")
    p.add_argument("--as-of", type=int, default=None)
    p.set_defaults(func=cmd_groups)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
