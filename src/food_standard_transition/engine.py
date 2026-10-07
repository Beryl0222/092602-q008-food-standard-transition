"""判定引擎：把某批次在两个真实时点（生产时、目标日期）适用的规则，
与配方快照、检测结果、标签声明、豁免逐项计算成可追溯的结论。

设计约束：

* 引擎不读写数据库，所有输入由服务层按"流程是否结束"过滤后传入；
* 缺少必需检测时不出结论（流程仍开放，等待后补检测）；
* 矛盾依据、漂移锁定、方法不可比、单位无换算依据 → 停止判定，由服务层留档；
* 结论只由引擎产生证据内容，持久化版本号、只追加等由服务层负责。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .domain import (
    Batch,
    DecisionResult,
    Exemption,
    ExemptionStatus,
    FormulaSnapshot,
    LabelDeclaration,
    TestResult,
    TransitionPolicy,
)
from .rules import (
    RuleBook,
    RuleResolutionError,
    RuleView,
    ResolvedLimit,
)
from .units import UnitConversionError, align_to_clause


class JudgmentStopped(Exception):
    """判定必须停止（矛盾/锁定/缺依据），不得出具合规性结论。"""

    def __init__(self, kind: str, message: str, detail: dict) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


@dataclass
class Judgment:
    result: str
    headline: str
    production_result: str
    target_result: str
    items: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    production_date: str = ""
    target_date: str = ""

    def evidence(self) -> str:
        return json.dumps(
            {
                "production_date": self.production_date,
                "target_date": self.target_date,
                "production_result": self.production_result,
                "target_result": self.target_result,
                "notes": self.notes,
                "items": self.items,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _bound_in_mg_per_100g(value: float | None, unit: str, energy: float | None,
                          indicator: str = "") -> float | None:
    if value is None:
        return None
    aligned = align_to_clause(value, unit, "mg/100g", indicator=indicator,
                              energy_kcal_per_100g=energy)
    return aligned.value


class Engine:
    def __init__(self, rulebook: RuleBook) -> None:
        self.rulebook = rulebook

    # -- 视图选择 ----------------------------------------------------------

    def _view(self, category: str, as_of: str, policy: TransitionPolicy | None) -> RuleView:
        try:
            if policy is None:
                return self.rulebook.active_view(category, as_of)
            return self.rulebook.lineage_view(category, as_of, policy.enforcement_date)
        except RuleResolutionError as exc:
            raise JudgmentStopped(exc.kind, str(exc), exc.detail) from exc

    # -- 证据小工具 --------------------------------------------------------

    @staticmethod
    def _exempt_for(exemptions: list[Exemption], clause_id: str, batch: Batch) -> Exemption | None:
        for exemption in exemptions:
            if exemption.status != ExemptionStatus.APPROVED or exemption.clause_id != clause_id:
                continue
            if exemption.scope == "lot" and exemption.scope_value == batch.lot_id:
                return exemption
            if exemption.scope == "category" and exemption.scope_value == batch.category:
                return exemption
        return None

    @staticmethod
    def _latest_test(tests: list[TestResult], indicator: str) -> TestResult | None:
        candidates = [t for t in tests if t.indicator == indicator]
        return max(candidates, key=lambda t: t.tested_at) if candidates else None

    def _clause_limits_view(
        self, view: RuleView, batch: Batch, snapshot: FormulaSnapshot | None
    ) -> dict[str, list[ResolvedLimit]]:
        """按指标归组并列限值；不同标准的强制方法必须可比、限值区间必须有交集。"""
        groups: dict[str, list[ResolvedLimit]] = {}
        for resolved in view.limits:
            groups.setdefault(resolved.clause.indicator, []).append(resolved)

        energy = snapshot.energy_kcal_per_100g if snapshot else None
        for indicator, resolutions in groups.items():
            methods = {r.clause.method_ref for r in resolutions if r.clause.method_ref}
            method_list = sorted(methods)
            for i in range(len(method_list)):
                for j in range(i + 1, len(method_list)):
                    if not self.rulebook.methods_comparable(method_list[i], method_list[j], indicator):
                        raise JudgmentStopped(
                            "method_conflict",
                            f"指标 {indicator} 存在两个互不可比的强制检验方法版本："
                            f"{method_list[i]} 与 {method_list[j]}",
                            {
                                "indicator": indicator,
                                "methods": method_list,
                                "clauses": [r.clause.clause_id for r in resolutions],
                                "as_of": view.as_of,
                            },
                        )
            # 把各限值区间折算到 mg/100g 检查交集
            intervals: list[tuple[float | None, float | None, str]] = []
            try:
                for r in resolutions:
                    c = r.clause
                    intervals.append(
                        (
                            _bound_in_mg_per_100g(c.min_value, c.unit, energy, indicator),
                            _bound_in_mg_per_100g(c.max_value, c.unit, energy, indicator),
                            c.clause_id,
                        )
                    )
            except UnitConversionError as exc:
                raise JudgmentStopped(
                    "unit_incompatible",
                    f"指标 {indicator} 的并列限值缺少共同换算基础：{exc}",
                    {"indicator": indicator, "reason": str(exc), "as_of": view.as_of},
                ) from exc
            low = max((lo for lo, _, _ in intervals if lo is not None), default=None)
            high = min((hi for _, hi, _ in intervals if hi is not None), default=None)
            if low is not None and high is not None and low > high:
                raise JudgmentStopped(
                    "contradiction",
                    f"指标 {indicator} 在 {view.as_of} 同时有效的依据互相矛盾："
                    f"下限 {low:g} mg/100g 高于上限 {high:g} mg/100g",
                    {
                        "indicator": indicator,
                        "intervals_mg_per_100g": [
                            {"min": lo, "max": hi, "clause_id": cid} for lo, hi, cid in intervals
                        ],
                        "as_of": view.as_of,
                    },
                )
        return groups

    # -- 限值判定 ----------------------------------------------------------

    def _evaluate_limits(
        self,
        stage: str,
        view: RuleView,
        batch: Batch,
        snapshot: FormulaSnapshot | None,
        tests: list[TestResult],
        exemptions: list[Exemption],
        items: list[dict],
        missing: list[str],
    ) -> str:
        groups = self._clause_limits_view(view, batch, snapshot)
        energy = snapshot.energy_kcal_per_100g if snapshot else None
        stage_failed = False
        for indicator in sorted(groups):
            resolutions = groups[indicator]
            exempt = next(
                (ex for r in resolutions
                 if (ex := self._exempt_for(exemptions, r.clause.clause_id, batch)) is not None),
                None,
            )
            test = self._latest_test(tests, indicator)
            bases = [
                {
                    "clause_id": r.clause.clause_id,
                    "clause_no": r.clause.clause_no,
                    "package_id": r.provenance.package_id,
                    "version": r.provenance.version,
                    "title": r.clause.title,
                    "min_value": r.clause.min_value,
                    "max_value": r.clause.max_value,
                    "unit": r.clause.unit,
                    "basis": r.clause.basis,
                    "method_ref": r.clause.method_ref,
                    "effective_date": r.provenance.package_effective_date,
                    "trail": list(r.provenance.trail),
                }
                for r in resolutions
            ]
            if exempt is not None:
                items.append({
                    "stage": stage, "kind": "limit", "indicator": indicator,
                    "status": "exempt", "bases": bases,
                    "exemption_id": exempt.exemption_id, "approved_by": exempt.approved_by,
                    "note": f"已批准豁免 {exempt.exemption_id} 免除该指标在本批次的判定",
                })
                continue
            if test is None:
                missing.append(indicator)
                items.append({
                    "stage": stage, "kind": "limit", "indicator": indicator,
                    "status": "missing_test", "bases": bases,
                    "note": "缺少该指标的检测结果，流程未结束时可补检测；当前不出结论",
                })
                continue

            required_methods = sorted({r.clause.method_ref for r in resolutions if r.clause.method_ref})
            method_ok = all(
                self.rulebook.methods_comparable(test.method_id, m, indicator) for m in required_methods
            )
            if not method_ok:
                raise JudgmentStopped(
                    "method_conflict",
                    f"批次 {batch.lot_id} 的 {indicator} 使用检验方法 {test.method_id}，"
                    f"与强制方法 {('、'.join(required_methods))} 不可比",
                    {
                        "lot_id": batch.lot_id,
                        "indicator": indicator,
                        "test_id": test.test_id,
                        "test_method": test.method_id,
                        "required_methods": required_methods,
                        "as_of": view.as_of,
                    },
                )

            # 以第一个依据的单位作为比较单位；交集已在 mg/100g 下验证过
            target_unit = resolutions[0].clause.unit
            try:
                aligned = align_to_clause(
                    test.value, test.unit, target_unit,
                    indicator=indicator, energy_kcal_per_100g=energy,
                )
            except UnitConversionError as exc:
                raise JudgmentStopped(
                    "unit_incompatible",
                    f"批次 {batch.lot_id} 的 {indicator} 检测单位 {test.unit} "
                    f"无法换算到条款单位 {target_unit}：{exc}",
                    {"lot_id": batch.lot_id, "indicator": indicator, "reason": str(exc)},
                ) from exc

            value = aligned.value
            failures: list[str] = []
            for r in resolutions:
                c = r.clause
                lo = align_to_clause(c.min_value, c.unit, target_unit, indicator=indicator,
                                     energy_kcal_per_100g=energy).value if c.min_value is not None else None
                hi = align_to_clause(c.max_value, c.unit, target_unit, indicator=indicator,
                                     energy_kcal_per_100g=energy).value if c.max_value is not None else None
                if lo is not None and value < lo:
                    failures.append(f"{c.clause_id} 要求 ≥ {lo:g} {target_unit}")
                if hi is not None and value > hi:
                    failures.append(f"{c.clause_id} 要求 ≤ {hi:g} {target_unit}")
            status = "fail" if failures else "pass"
            stage_failed = stage_failed or bool(failures)
            items.append({
                "stage": stage, "kind": "limit", "indicator": indicator, "status": status,
                "bases": bases,
                "test": {
                    "test_id": test.test_id, "value": test.value, "unit": test.unit,
                    "method_id": test.method_id, "tested_at": test.tested_at,
                    "received_at": test.received_at,
                },
                "aligned_value": value, "aligned_unit": target_unit,
                "conversion_steps": list(aligned.steps),
                "note": "；".join(failures) or f"{value:g} {target_unit} 满足全部有效依据",
            })
        return "fail" if stage_failed else ("incomplete" if missing else "pass")

    # -- 标签判定 ----------------------------------------------------------

    @staticmethod
    def _evaluate_labels(
        stage: str,
        view: RuleView,
        batch: Batch,
        declarations: dict[str, LabelDeclaration],
        exemptions: list[Exemption],
        items: list[dict],
        grace_until: str = "",
    ) -> str:
        worst = "pass"
        for resolved in view.labels:
            clause = resolved.clause
            exemption = Engine._exempt_for(exemptions, clause.clause_id, batch)
            declaration = declarations.get(clause.label_code)
            present = declaration.present if declaration else False
            basis = {
                "clause_id": clause.clause_id,
                "clause_no": clause.clause_no,
                "package_id": resolved.provenance.package_id,
                "version": resolved.provenance.version,
                "title": clause.title,
                "label_code": clause.label_code,
                "label_required": clause.label_required,
                "effective_date": resolved.provenance.package_effective_date,
                "trail": list(resolved.provenance.trail),
            }
            if exemption is not None:
                items.append({
                    "stage": stage, "kind": "label", "label_code": clause.label_code,
                    "status": "exempt", "basis": basis,
                    "exemption_id": exemption.exemption_id, "approved_by": exemption.approved_by,
                    "note": f"已批准豁免 {exemption.exemption_id}",
                })
                continue
            satisfied = present if clause.label_required else not present
            if satisfied:
                items.append({
                    "stage": stage, "kind": "label", "label_code": clause.label_code,
                    "status": "pass", "basis": basis,
                    "declared": present,
                    "note": "标签声明与条款一致",
                })
                continue

            within_grace = bool(grace_until) and view.as_of <= grace_until
            if within_grace:
                worst = "relabel" if worst == "pass" else worst
                items.append({
                    "stage": stage, "kind": "label", "label_code": clause.label_code,
                    "status": "relabel", "basis": basis,
                    "declared": present, "grace_until": grace_until,
                    "note": f"标签不符合新要求，但旧标签库存允许使用至 {grace_until}，限期换标",
                })
            else:
                worst = "fail"
                items.append({
                    "stage": stage, "kind": "label", "label_code": clause.label_code,
                    "status": "fail", "basis": basis,
                    "declared": present,
                    **({"grace_until": grace_until} if grace_until else {}),
                    "note": "标签声明不符合强制要求且已过旧标签使用期限",
                })
        return worst

    # -- 主流程 ------------------------------------------------------------

    def judge(
        self,
        batch: Batch,
        snapshot: FormulaSnapshot | None,
        labels: list[LabelDeclaration],
        tests: list[TestResult],
        exemptions: list[Exemption],
        target_date: str,
        policy: TransitionPolicy | None = None,
    ) -> Judgment:
        declarations = {d.label_code: d for d in labels}
        notes: list[str] = []

        production_view = self._view(batch.category, batch.production_date, policy)
        notes.extend(production_view.notes)

        # 目标日期的限值视图：切换前生产的库存按 stock_new_limits_from 适用新限值
        limit_as_of = target_date
        if policy is not None and batch.production_date < policy.enforcement_date:
            switch_stock = policy.stock_new_limits_from or policy.enforcement_date
            if target_date < switch_stock:
                limit_as_of = batch.production_date
                notes.append(
                    f"库存过渡：批次生产于 {batch.production_date}（早于强制切换日 "
                    f"{policy.enforcement_date}），目标日 {target_date} 尚未到库存新限值日 "
                    f"{switch_stock}，限值仍按生产时规则判定"
                )
        limit_view = self._view(batch.category, limit_as_of, policy)
        label_view = self._view(batch.category, target_date, policy)
        notes.extend(label_view.notes)

        items: list[dict] = []
        missing: list[str] = []

        production_limit = self._evaluate_limits(
            "production", production_view, batch, snapshot, tests, exemptions, items, missing
        )
        target_limit = self._evaluate_limits(
            "target", limit_view, batch, snapshot, tests, exemptions, items, missing
        )
        production_label = self._evaluate_labels(
            "production", production_view, batch, declarations, exemptions, items
        )
        grace = policy.old_label_use_until if policy else ""
        target_label = self._evaluate_labels(
            "target", label_view, batch, declarations, exemptions, items, grace_until=grace
        )

        if missing:
            raise JudgmentStopped(
                "missing_test",
                f"批次 {batch.lot_id} 缺少指标检测结果：{'、'.join(sorted(set(missing)))}",
                {"lot_id": batch.lot_id, "missing": sorted(set(missing))},
            )

        limit_failed = "fail" in (production_limit, target_limit)
        if limit_failed or production_label == "fail" or target_label == "fail":
            result = DecisionResult.HALT
            headline = "停止流转：存在不符合强制性限值或标签要求的项目"
        elif "relabel" in (production_label, target_label):
            result = DecisionResult.RELABEL
            headline = "换标：限值项目合规，标签须在旧标签库存期限前更换"
        elif target_label == "relabel" or production_label == "relabel":
            result = DecisionResult.RELABEL
            headline = "换标：限值项目合规，标签须在旧标签库存期限前更换"
        else:
            result = DecisionResult.COMPLIANT
            headline = "合规：生产时与目标日期适用的限值、标签项目全部满足"

        return Judgment(
            result=result,
            headline=headline,
            production_result="fail" if limit_failed or production_label == "fail" else "pass",
            target_result=result,
            items=items,
            notes=notes,
            production_date=batch.production_date,
            target_date=target_date,
        )

    # -- 复核影响分析 ------------------------------------------------------

    @staticmethod
    def impact(previous_evidence: dict, judgment: Judgment) -> list[dict]:
        """逐项对比原结论证据与新判定，说明后续修改对原结论的影响。"""
        old_items = previous_evidence.get("items", [])
        changes: list[dict] = []

        def key(item: dict) -> tuple[str, str, str]:
            return (
                item.get("stage", ""),
                item.get("indicator") or item.get("label_code") or "",
                item.get("kind", ""),
            )

        old_by_key = {key(item): item for item in old_items}
        for new_item in judgment.items:
            old_item = old_by_key.get(key(new_item))
            if old_item is None:
                changes.append({
                    "item": key(new_item), "change": "added",
                    "new_status": new_item.get("status"),
                    "note": "新依据增加的判定项目",
                })
                continue
            old_bases = {b["clause_id"] for b in old_item.get("bases", [])} or {old_item.get("basis", {}).get("clause_id")}
            new_bases = {b["clause_id"] for b in new_item.get("bases", [])} or {new_item.get("basis", {}).get("clause_id")}
            old_bases.discard(None)
            new_bases.discard(None)
            if old_item.get("status") != new_item.get("status"):
                kind = "status_changed"
            elif old_bases != new_bases:
                kind = "basis_changed"
            elif old_item.get("aligned_value") != new_item.get("aligned_value"):
                kind = "value_rebased"
            else:
                kind = "unchanged"
            if kind == "unchanged":
                continue
            changes.append({
                "item": list(key(new_item)),
                "change": kind,
                "old_status": old_item.get("status"),
                "new_status": new_item.get("status"),
                "old_clauses": sorted(old_bases),
                "new_clauses": sorted(new_bases),
                "note": new_item.get("note", ""),
            })
        new_keys = {key(i) for i in judgment.items}
        for old_item in old_items:
            if key(old_item) not in new_keys:
                changes.append({
                    "item": list(key(old_item)), "change": "removed",
                    "old_status": old_item.get("status"),
                    "note": "依据已被修改单废止或不再适用",
                })
        return changes
