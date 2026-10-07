"""食品标准迁移判定引擎命令行。

所有结果以 JSON 打印到标准输出；错误打印到标准错误并以非零状态退出。
数据库默认使用当前目录的 ``fst.db``，可用 ``--db`` 或环境变量 ``FST_DB`` 覆盖。

退出码：

* ``0`` 正常，包括出具了"停止流转"这类明确结论；
* ``3`` 判定停止（矛盾依据 / 漂移锁定 / 缺检测），未出具结论；
* ``1`` 输入或业务规则错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .service import Service, ServiceError
from .store import Store


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _service(args: argparse.Namespace) -> Service:
    db_path = getattr(args, "db", None) or os.environ.get("FST_DB", "fst.db")
    return Service(Store(db_path))


def _fail(message: str, code: int = 1, detail: object = None) -> int:
    payload = {"error": message}
    if detail is not None:
        payload["detail"] = detail
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="food-standard-transition",
        description="食品标准迁移判定引擎：按批次真实时点选择规则并出具可追溯结论",
    )
    parser.add_argument("--db", help="SQLite 数据库路径（默认 fst.db 或环境变量 FST_DB）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="健康检查")

    p_validate = sub.add_parser("validate", help="兼容旧版：登记一条基础记录")
    p_validate.add_argument("file")

    p_import = sub.add_parser("import", help="导入规则包、检验方法与过渡政策（重复导入幂等）")
    p_import.add_argument("files", nargs="+", help="一个或多个规则包 JSON")

    p_product = sub.add_parser("product", help="导入/更新产品批次、配方快照、标签声明与检测")
    p_product.add_argument("files", nargs="+")

    p_test = sub.add_parser("add-test", help="向尚未结束的流程后补检测")
    p_test.add_argument("file", help="检测结果 JSON（含 lot_id）")

    p_judge = sub.add_parser("judge", help="对批次按生产日期与目标日期出具首版结论")
    p_judge.add_argument("--lot", required=True)
    p_judge.add_argument("--date", required=True, help="目标日期 YYYY-MM-DD")

    p_review = sub.add_parser("review", help="以新版本复核原结论，不原地覆盖")
    p_review.add_argument("--lot", required=True)
    p_review.add_argument("--date", required=True)

    p_decision = sub.add_parser("decision", help="查看批次最新结论与逐项依据")
    p_decision.add_argument("--lot", required=True)
    p_decision.add_argument("--items", action="store_true", help="展开逐项条款时点/换算/方法")

    p_versions = sub.add_parser("versions", help="列出批次的全部结论版本")
    p_versions.add_argument("--lot", required=True)

    p_conflicts = sub.add_parser("conflicts", help="查看停止判定时保留的矛盾/锁定记录")
    p_conflicts.add_argument("--lot", default="")

    p_rules = sub.add_parser("rules", help="查看某品类在某时点实际适用的条款")
    p_rules.add_argument("--category", required=True)
    p_rules.add_argument("--date", required=True)

    p_drifts = sub.add_parser("drifts", help="查看同编号不同内容的漂移锁定")
    p_drifts.add_argument("--status", default="", choices=["", "locked", "resolved"])

    p_resolve = sub.add_parser("resolve-drift", help="处置并解除漂移锁定")
    p_resolve.add_argument("--id", required=True, dest="drift_id")
    p_resolve.add_argument("--resolution", required=True)
    p_resolve.add_argument("--by", required=True)

    p_exdraft = sub.add_parser("exemption-draft", help="起草豁免（草案不生效）")
    p_exdraft.add_argument("file")

    p_exdecide = sub.add_parser("exemption-decide", help="批准/驳回豁免（批准人不得是起草人）")
    p_exdecide.add_argument("--id", required=True, dest="exemption_id")
    p_exdecide.add_argument("--by", required=True)
    group = p_exdecide.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")
    p_exdecide.add_argument("--reason", default="")

    p_migrate = sub.add_parser("migrate", help="批量迁移；中途退出后重跑从剩余批次继续")
    p_migrate.add_argument("--campaign", required=True)
    p_migrate.add_argument("--date", required=True)
    p_migrate.add_argument("--lots", default="", help="逗号分隔的批次编号")
    p_migrate.add_argument("--lots-file", default="", help="文本文件，每行一个批次编号")
    p_migrate.add_argument("--limit", type=int, default=None, help="本轮最多处理批次数")

    p_mstatus = sub.add_parser("migration-status", help="查看批量迁移检查点")
    p_mstatus.add_argument("--campaign", required=True)

    return parser


def _lots(args: argparse.Namespace) -> list[str]:
    lots: list[str] = []
    if args.lots:
        lots.extend(part.strip() for part in args.lots.split(",") if part.strip())
    if args.lots_file:
        lots.extend(
            line.strip() for line in Path(args.lots_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if not lots:
        raise ServiceError("invalid_input", "migrate 需要通过 --lots 或 --lots-file 提供批次")
    # 去重保序
    return list(dict.fromkeys(lots))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "health":
        _print(Service(Store(":memory:")).health())
        return 0

    try:
        if args.command == "validate":
            service = _service(args)
            _print(service.register(_load_json(args.file)))
            return 0

        if args.command == "import":
            service = _service(args)
            merged = {"packages": [], "methods": [], "equivalences": [], "policies": []}
            for path in args.files:
                payload = _load_json(path)
                for key in merged:
                    merged[key].extend(payload.get(key, []))
            _print(service.import_bundle(merged))
            return 0

        if args.command == "product":
            service = _service(args)
            outcomes = [service.import_product(_load_json(path)) for path in args.files]
            _print(outcomes if len(outcomes) > 1 else outcomes[0])
            return 0

        if args.command == "add-test":
            service = _service(args)
            _print(service.add_test(_load_json(args.file)))
            return 0

        if args.command == "judge":
            service = _service(args)
            outcome = service.judge(args.lot, args.date)
            _print(outcome)
            return 3 if outcome["status"] == "stopped" else 0

        if args.command == "review":
            service = _service(args)
            outcome = service.review(args.lot, args.date)
            _print(outcome)
            return 3 if outcome["status"] == "stopped" else 0

        if args.command == "decision":
            service = _service(args)
            outcome = service.decision(args.lot)
            if outcome is None:
                return _fail(f"批次 {args.lot} 尚无结论")
            if not args.items:
                outcome = {k: v for k, v in outcome.items() if k != "items"}
            _print(outcome)
            return 0

        if args.command == "versions":
            _print(_service(args).decision_versions(args.lot))
            return 0

        if args.command == "conflicts":
            _print(_service(args).conflicts(args.lot))
            return 0

        if args.command == "rules":
            _print(_service(args).applicable_rules(args.category, args.date))
            return 0

        if args.command == "drifts":
            _print(_service(args).list_drifts(args.status or None))
            return 0

        if args.command == "resolve-drift":
            _print(_service(args).resolve_drift(args.drift_id, args.resolution, args.by))
            return 0

        if args.command == "exemption-draft":
            _print(_service(args).draft_exemption(_load_json(args.file)))
            return 0

        if args.command == "exemption-decide":
            _print(_service(args).decide_exemption(
                args.exemption_id, args.by, args.approve, args.reason))
            return 0

        if args.command == "migrate":
            service = _service(args)
            outcome = service.migrate(args.campaign, _lots(args), args.date, args.limit)
            _print(outcome)
            return 0

        if args.command == "migration-status":
            _print(_service(args).migration_status(args.campaign))
            return 0

    except ServiceError as exc:
        return _fail(str(exc), detail={"kind": exc.kind, **exc.detail})
    except FileNotFoundError as exc:
        return _fail(f"文件不存在：{exc.filename}")
    except KeyError as exc:
        return _fail(f"载荷缺少字段：{exc.args[0]}")
    except (json.JSONDecodeError, ValueError) as exc:
        return _fail(f"输入无效：{exc}")

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
