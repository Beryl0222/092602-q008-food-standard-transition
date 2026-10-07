"""食品标准迁移判定引擎的应用服务。

负责把外部载荷装配成领域记录、保证导入幂等与漂移锁定、编排判定与复核、
维护豁免职责分离，并驱动可从中断处继续的批量迁移。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter

from .domain import (
    Amendment,
    Batch,
    Clause,
    ConflictRecord,
    Decision,
    DecisionResult,
    Drift,
    DriftStatus,
    Exemption,
    ExemptionStatus,
    FlowState,
    FormulaSnapshot,
    LabelDeclaration,
    Method,
    MethodEquivalence,
    MigrationStatus,
    Record,
    Replacement,
    StandardPackage,
    TestResult,
    TransitionPolicy,
    now_stamp,
)
from .engine import Engine, Judgment, JudgmentStopped
from .rules import RuleBook
from .store import Store, _dumps

_RESULT_CN = {
    DecisionResult.COMPLIANT: "合规",
    DecisionResult.RELABEL: "换标",
    DecisionResult.HALT: "停止流转",
    DecisionResult.STOPPED: "判定停止",
}


class ServiceError(Exception):
    def __init__(self, kind: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail or {}


def _fingerprint(obj: object) -> str:
    body = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical_clause(data: dict) -> dict:
    keys = (
        "clause_no", "title", "kind", "categories", "indicator", "basis",
        "min_value", "max_value", "unit", "method_ref", "label_code",
        "label_required", "note",
    )
    return {k: data.get(k, "" if k in ("title", "note", "indicator", "basis", "unit",
                                       "method_ref", "label_code") else None) for k in keys}


class Service:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # -- 基线能力（保持兼容） ----------------------------------------------

    def health(self) -> dict[str, str]:
        return {"service": "food_standard_transition", "status": "ok"}

    def register(self, payload: dict[str, object]) -> dict[str, object]:
        required = ("record_id", "owner_id", "state")
        missing = [name for name in required if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError("缺少必要字段：" + "、".join(missing))
        record = Record(
            record_id=str(payload["record_id"]), owner_id=str(payload["owner_id"]),
            state=str(payload["state"]), revision=int(payload.get("revision", 1)),
        )
        return self.store.add(record).__dict__.copy()

    def find(self, record_id: str) -> dict[str, object] | None:
        value = self.store.get(record_id)
        return value.__dict__.copy() if value else None

    # -- 规则库装配 --------------------------------------------------------

    def _rulebook(self) -> RuleBook:
        return RuleBook(
            packages=self.store.list_packages(),
            clauses=self.store.list_base_clauses(),
            amendments=self.store.list_amendments(),
            policies=self.store.list_policies(),
            methods=self.store.list_methods(),
            equivalences=self.store.list_equivalences(),
            drifts=self.store.list_drifts(),
        )

    # -- 规则包导入 --------------------------------------------------------

    def import_bundle(self, payload: dict) -> dict:
        """导入规则包、方法与过渡政策。

        返回每个包的导入状态：identical（内容一致，幂等跳过）、imported（新写入）、
        drift_locked（同编号同版本内容不同，登记漂移并锁定，原内容不被覆盖）。
        """
        stamp = now_stamp()
        results: list[dict] = []

        for pkg_data in payload.get("packages", []):
            package_id = str(pkg_data["package_id"])
            version = str(pkg_data["version"])
            clause_rows = pkg_data.get("clauses", [])
            amendment_rows = pkg_data.get("amendments", [])

            canonical = {
                "package_id": package_id,
                "version": version,
                "title": pkg_data.get("title", ""),
                "summary": pkg_data.get("summary", ""),
                "effective_date": pkg_data["effective_date"],
                "repeal_date": pkg_data.get("repeal_date", ""),
                "categories": sorted(pkg_data.get("categories", [])),
                "clauses": [_canonical_clause(c) for c in sorted(clause_rows, key=lambda c: c["clause_no"])],
                "amendments": [
                    {
                        "number": a.get("number", ""),
                        "effective_date": a["effective_date"],
                        "summary": a.get("summary", ""),
                        "replacements": [
                            {
                                "clause_no": r["clause_no"],
                                "action": r["action"],
                                "clause": None if r.get("action") == "repeal"
                                else _canonical_clause(r["clause"]),
                            }
                            for r in sorted(a.get("replacements", []), key=lambda r: r["clause_no"])
                        ],
                    }
                    for a in sorted(amendment_rows, key=lambda a: (a["effective_date"], a.get("number", "")))
                ],
            }
            fingerprint = _fingerprint(canonical)

            if self.store.package_version_exists(package_id, version):
                existing = self.store.package_fingerprint(package_id, version)
                if existing == fingerprint:
                    results.append({
                        "package_id": package_id, "version": version,
                        "status": "identical",
                        "note": "编号、版本与内容指纹一致，重复导入不产生新版本",
                    })
                    continue
                # 同编号不同内容：登记漂移并锁定，原内容保持不变
                if not self.store.drift_exists_open(package_id, version, fingerprint):
                    drift = Drift(
                        drift_id=f"DRIFT-{uuid.uuid4().hex[:12]}",
                        package_id=package_id, version=version,
                        existing_fingerprint=existing, incoming_fingerprint=fingerprint,
                        detected_at=stamp,
                    )
                    self.store.add_drift(drift)
                    drift_id = drift.drift_id
                else:
                    drift_id = self.store.find_open_drift(
                        package_id, version, fingerprint
                    ).drift_id
                results.append({
                    "package_id": package_id, "version": version,
                    "status": "drift_locked", "drift_id": drift_id,
                    "existing_fingerprint": existing, "incoming_fingerprint": fingerprint,
                    "note": "同编号同版本但内容不同，已登记漂移；依赖该标准的判定一律停止，"
                            "原内容不被覆盖，需先解决漂移",
                })
                continue

            package = StandardPackage(
                package_id=package_id, version=version,
                title=str(pkg_data.get("title", "")), summary=str(pkg_data.get("summary", "")),
                effective_date=str(pkg_data["effective_date"]),
                fingerprint=fingerprint, imported_at=stamp,
                repeal_date=str(pkg_data.get("repeal_date", "")),
                categories=tuple(pkg_data.get("categories", [])),
            )
            self.store.add_package(package)

            for row in clause_rows:
                clause = self._build_clause(
                    row, package_id, version,
                    clause_id=str(row.get("clause_id") or f"{package_id}@{version}:{row['clause_no']}"),
                )
                if self.store.clause_exists(clause.clause_id):
                    continue
                self.store.add_clause(clause)

            for a_index, a_row in enumerate(amendment_rows, start=1):
                amendment_id = str(a_row.get("amendment_id") or f"AMD-{package_id}-{version}-{a_index}")
                if self.store.amendment_exists(amendment_id):
                    continue
                replacements: list[Replacement] = []
                for r_index, r_row in enumerate(a_row.get("replacements", []), start=1):
                    if r_row["action"] == "repeal":
                        replacements.append(Replacement(clause_no=str(r_row["clause_no"]), action="repeal"))
                        continue
                    child = self._build_clause(
                        r_row["clause"], package_id, version,
                        clause_id=str(
                            r_row["clause"].get("clause_id")
                            or f"{package_id}@{version}:{r_row['clause_no']}#{amendment_id}"
                        ),
                    )
                    if not self.store.clause_exists(child.clause_id):
                        self.store.add_clause(child, origin="amendment", amendment_id=amendment_id)
                    replacements.append(Replacement(
                        clause_no=str(r_row["clause_no"]), action="replace", clause=child,
                    ))
                self.store.add_amendment(Amendment(
                    amendment_id=amendment_id, package_id=package_id, version=version,
                    number=str(a_row.get("number", amendment_id)),
                    effective_date=str(a_row["effective_date"]),
                    summary=str(a_row.get("summary", "")),
                    replacements=tuple(replacements),
                ))

            results.append({
                "package_id": package_id, "version": version,
                "status": "imported", "fingerprint": fingerprint,
                "clauses": len(clause_rows), "amendments": len(amendment_rows),
            })

        for method in payload.get("methods", []):
            self.store.add_method(Method(**{k: method.get(k, "") for k in (
                "method_id", "code", "version", "indicator", "title", "issued_date")}))
        for edge in payload.get("equivalences", []):
            self.store.add_equivalence(MethodEquivalence(
                method_id=edge["method_id"], other_method_id=edge["other_method_id"],
                indicator=edge["indicator"], comparable=bool(edge["comparable"]),
                note=edge.get("note", ""),
            ))
        for policy in payload.get("policies", []):
            self.store.add_policy(TransitionPolicy(**{
                k: policy.get(k, "") for k in (
                    "policy_id", "category", "old_package_id", "old_version",
                    "new_package_id", "new_version", "new_effective_date",
                    "enforcement_date", "old_label_use_until", "stock_new_limits_from", "note")
            }))

        summary = Counter(r["status"] for r in results)
        return {
            "imported_at": stamp,
            "packages": results,
            "methods": len(payload.get("methods", [])),
            "equivalences": len(payload.get("equivalences", [])),
            "policies": len(payload.get("policies", [])),
            "summary": dict(summary),
        }

    @staticmethod
    def _build_clause(row: dict, package_id: str, version: str, clause_id: str) -> Clause:
        kind = row.get("kind", "limit")
        return Clause(
            clause_id=clause_id, package_id=package_id, version=version,
            clause_no=str(row["clause_no"]), title=str(row.get("title", "")), kind=kind,
            categories=tuple(row.get("categories", [])),
            indicator=str(row.get("indicator", "")),
            basis=str(row.get("basis", "per_100g")),
            min_value=row.get("min_value"), max_value=row.get("max_value"),
            unit=str(row.get("unit", "")), method_ref=str(row.get("method_ref", "")),
            label_code=str(row.get("label_code", "")),
            label_required=bool(row.get("label_required", True)),
            note=str(row.get("note", "")),
        )

    def list_drifts(self, status: str | None = None) -> list[dict]:
        return [d.__dict__.copy() for d in self.store.list_drifts(status)]

    def resolve_drift(self, drift_id: str, resolution: str, approver: str) -> dict:
        drifts = [d for d in self.store.list_drifts() if d.drift_id == drift_id]
        if not drifts:
            raise ServiceError("not_found", f"漂移记录不存在：{drift_id}")
        drift = drifts[0]
        if drift.status != DriftStatus.LOCKED:
            raise ServiceError("already_resolved", f"漂移 {drift_id} 已解决")
        if not resolution.strip() or not approver.strip():
            raise ServiceError("invalid_input", "解决漂移必须给出处置说明与负责人")
        self.store.resolve_drift(
            drift_id, f"{resolution}（负责人：{approver}，时间：{now_stamp()}）"
        )
        return {"drift_id": drift_id, "status": DriftStatus.RESOLVED}

    # -- 产品批次 ----------------------------------------------------------

    def import_product(self, payload: dict) -> dict:
        b = payload["batch"]
        batch = Batch(
            lot_id=str(b["lot_id"]), product_name=str(b.get("product_name", "")),
            category=str(b["category"]), factory_id=str(b.get("factory_id", "")),
            production_date=str(b["production_date"]),
        )
        existing = self.store.get_batch(batch.lot_id)
        if existing is not None and existing.flow_state != FlowState.OPEN:
            raise ServiceError(
                "flow_closed",
                f"批次 {batch.lot_id} 已出具结论，基础信息不得改动，如需更正请发起复核",
                {"lot_id": batch.lot_id},
            )
        self.store.upsert_batch(batch)

        snapshot_id = ""
        if payload.get("snapshot"):
            s = payload["snapshot"]
            snapshot_id = str(s.get("snapshot_id") or f"SNAP-{batch.lot_id}")
            self.store.add_snapshot(FormulaSnapshot(
                snapshot_id=snapshot_id, lot_id=batch.lot_id,
                energy_kcal_per_100g=float(s["energy_kcal_per_100g"]),
                components=tuple((pair[0], pair[1]) for pair in s.get("components", [])),
                recorded_at=str(s.get("recorded_at", now_stamp())),
            ))
        for label in payload.get("labels", []):
            self.store.upsert_label(LabelDeclaration(
                lot_id=batch.lot_id, label_code=str(label["label_code"]),
                present=bool(label["present"]),
                label_version_date=str(label.get("label_version_date", "")),
            ))
        for test in payload.get("tests", []):
            self._insert_test(batch.lot_id, test)

        return {
            "lot_id": batch.lot_id, "category": batch.category,
            "factory_id": batch.factory_id, "production_date": batch.production_date,
            "snapshot_id": snapshot_id,
            "labels": len(payload.get("labels", [])),
            "tests": len(payload.get("tests", [])),
            "flow_state": (existing or batch).flow_state,
        }

    def _insert_test(self, lot_id: str, test: dict) -> str:
        batch = self.store.get_batch(lot_id)
        if batch is None:
            raise ServiceError("not_found", f"批次不存在：{lot_id}")
        if batch.flow_state != FlowState.OPEN:
            raise ServiceError(
                "flow_closed",
                f"批次 {lot_id} 已于 {batch.decided_at} 出具结论，检测通道关闭；"
                "后补检测只允许进入尚未结束的流程，更正请走复核",
                {"lot_id": lot_id, "decided_at": batch.decided_at},
            )
        test_id = str(test.get("test_id") or f"TEST-{uuid.uuid4().hex[:12]}")
        self.store.add_test(TestResult(
            test_id=test_id, lot_id=lot_id, indicator=str(test["indicator"]),
            value=float(test["value"]), unit=str(test["unit"]),
            method_id=str(test["method_id"]), tested_at=str(test["tested_at"]),
            received_at=str(test.get("received_at", now_stamp())),
        ))
        return test_id

    def add_test(self, payload: dict) -> dict:
        test_id = self._insert_test(str(payload["lot_id"]), payload)
        return {"test_id": test_id, "lot_id": payload["lot_id"], "accepted": True}

    # -- 豁免 --------------------------------------------------------------

    def draft_exemption(self, payload: dict) -> dict:
        required = ("scope", "scope_value", "clause_id", "drafted_by")
        missing = [k for k in required if not str(payload.get(k, "")).strip()]
        if missing:
            raise ServiceError("invalid_input", "豁免缺少字段：" + "、".join(missing))
        if payload["scope"] not in ("lot", "category"):
            raise ServiceError("invalid_input", "豁免范围只能是 lot 或 category")
        exemption_id = str(payload.get("exemption_id") or f"EXM-{uuid.uuid4().hex[:12]}")
        stamp = now_stamp()
        exemption = Exemption(
            exemption_id=exemption_id, scope=payload["scope"],
            scope_value=payload["scope_value"], clause_id=payload["clause_id"],
            reason=str(payload.get("reason", "")), drafted_by=payload["drafted_by"],
            status=ExemptionStatus.DRAFT, created_at=stamp,
        )
        self.store.add_exemption(exemption)
        return {"exemption_id": exemption_id, "status": ExemptionStatus.DRAFT,
                "note": "豁免为草案，起草人不得自行批准，需由其他人批准后方可作为依据"}

    def decide_exemption(self, exemption_id: str, approver: str, approve: bool, reason: str = "") -> dict:
        exemption = self.store.get_exemption(exemption_id)
        if exemption is None:
            raise ServiceError("not_found", f"豁免不存在：{exemption_id}")
        if exemption.status != ExemptionStatus.DRAFT:
            raise ServiceError("invalid_state", f"豁免状态为 {exemption.status}，不可再审批")
        if not approver.strip():
            raise ServiceError("invalid_input", "批准人不能为空")
        if approver == exemption.drafted_by:
            raise ServiceError(
                "segregation_of_duties",
                f"起草人 {approver} 不得独自批准豁免 {exemption_id}，必须由其他责任人批准",
                {"exemption_id": exemption_id, "drafted_by": exemption.drafted_by},
            )
        new_status = ExemptionStatus.APPROVED if approve else ExemptionStatus.REJECTED
        self.store.update_exemption_status(exemption_id, new_status, approver, now_stamp())
        return {"exemption_id": exemption_id, "status": new_status,
                "approved_by": approver if approve else "", "reason": reason}

    # -- 判定与复核 --------------------------------------------------------

    def _gather(self, lot_id: str):
        batch = self.store.get_batch(lot_id)
        if batch is None:
            raise ServiceError("not_found", f"批次不存在：{lot_id}")
        snapshot = self.store.snapshot_for_lot(lot_id)
        labels = self.store.labels_for_lot(lot_id)
        tests = self.store.tests_for_lot(lot_id)
        exemptions = self.store.exemptions_for(lot_id, batch.category)
        policy = self.store.policy_for_category(batch.category)
        return batch, snapshot, labels, tests, exemptions, policy

    def _stop_response(self, lot_id: str, exc: JudgmentStopped, persist: bool) -> dict:
        """停止判定的响应；只有矛盾/锁定类依据才落 conflicts，缺检测只等待补料。"""
        if not persist or exc.kind == "missing_test":
            return {
                "lot_id": lot_id, "status": DecisionResult.STOPPED, "kind": exc.kind,
                "message": exc.message, "detail": exc.detail,
                "persisted": False,
                "note": ("检测证据不全，流程仍开放，可通过 add-test 后补检测后重新判定"
                         if exc.kind == "missing_test" else ""),
            }
        detail = _dumps({"kind": exc.kind, "message": exc.message, **exc.detail})
        # 同一批次同一停止原因幂等：不重复留档
        for open_conflict in self.store.open_conflicts(lot_id):
            if open_conflict.kind == exc.kind and open_conflict.detail == detail:
                return {
                    "lot_id": lot_id, "status": DecisionResult.STOPPED, "kind": exc.kind,
                    "conflict_id": open_conflict.conflict_id, "message": exc.message,
                    "detail": exc.detail, "persisted": True,
                }
        conflict = ConflictRecord(
            conflict_id=f"CFL-{uuid.uuid4().hex[:12]}", lot_id=lot_id, kind=exc.kind,
            detail=detail, created_at=now_stamp(),
        )
        self.store.add_conflict(conflict)
        return {
            "lot_id": lot_id, "status": DecisionResult.STOPPED, "kind": exc.kind,
            "conflict_id": conflict.conflict_id, "message": exc.message, "detail": exc.detail,
            "persisted": True,
        }

    def _persist_judgment(
        self, batch: Batch, target_date: str, judgment: Judgment, review_of: Decision | None
    ) -> dict:
        version = self.store.next_decision_version(batch.lot_id)
        decision_id = f"DEC-{batch.lot_id}-v{version}"
        evidence = json.loads(judgment.evidence())
        impact: list[dict] = []
        if review_of is not None:
            previous_evidence = json.loads(review_of.evidence)
            impact = Engine.impact(previous_evidence, judgment)
            evidence["impact"] = impact
            evidence["review_of"] = review_of.decision_id
        decision = Decision(
            decision_id=decision_id, lot_id=batch.lot_id, version=version,
            result=judgment.result, production_date=batch.production_date,
            target_date=target_date, summary=judgment.headline,
            evidence=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            decided_at=now_stamp(),
            review_of=review_of.decision_id if review_of else "",
        )
        self.store.add_decision(decision)
        if batch.flow_state == FlowState.OPEN:
            self.store.mark_batch_decided(batch.lot_id, decision.decided_at)
        self.store.resolve_conflicts(batch.lot_id, decision_id)
        return {
            "lot_id": batch.lot_id, "status": judgment.result,
            "result_cn": _RESULT_CN[judgment.result],
            "decision_id": decision_id, "version": version,
            "headline": judgment.headline,
            "production_result": judgment.production_result,
            "target_result": judgment.target_result,
            "items": evidence["items"], "notes": evidence["notes"],
            **({"impact": impact, "review_of": review_of.decision_id,
                "previous_result": review_of.result} if review_of else {}),
        }

    def judge(self, lot_id: str, target_date: str) -> dict:
        batch, snapshot, labels, tests, exemptions, policy = self._gather(lot_id)
        if batch.flow_state != FlowState.OPEN:
            raise ServiceError(
                "flow_closed",
                f"批次 {lot_id} 已出具结论，不得原地覆盖；请使用 review 生成复核新版本",
                {"lot_id": lot_id, "latest": self.store.latest_decision(lot_id).decision_id},
            )
        engine = Engine(self._rulebook())
        try:
            judgment = engine.judge(batch, snapshot, labels, tests, exemptions, target_date, policy)
        except JudgmentStopped as exc:
            return self._stop_response(lot_id, exc, persist=True)
        return self._persist_judgment(batch, target_date, judgment, None)

    def review(self, lot_id: str, target_date: str) -> dict:
        batch, snapshot, labels, tests, exemptions, policy = self._gather(lot_id)
        previous = self.store.latest_decision(lot_id)
        if previous is None:
            raise ServiceError("not_found", f"批次 {lot_id} 尚无原结论，应先 judge 而不是 review")
        engine = Engine(self._rulebook())
        try:
            judgment = engine.judge(batch, snapshot, labels, tests, exemptions, target_date, policy)
        except JudgmentStopped as exc:
            stopped = self._stop_response(lot_id, exc, persist=True)
            stopped["previous_decision"] = previous.decision_id
            stopped["previous_result"] = previous.result
            stopped["note"] = "复核因依据冲突/锁定而停止，原结论保留不变"
            return stopped
        result = self._persist_judgment(batch, target_date, judgment, previous)
        result["result_changed"] = previous.result != judgment.result
        return result

    # -- 查询 --------------------------------------------------------------

    def decision(self, lot_id: str) -> dict | None:
        decision = self.store.latest_decision(lot_id)
        if decision is None:
            return None
        evidence = json.loads(decision.evidence)
        return {
            "decision_id": decision.decision_id, "lot_id": lot_id,
            "version": decision.version, "result": decision.result,
            "result_cn": _RESULT_CN.get(decision.result, decision.result),
            "production_date": decision.production_date,
            "target_date": decision.target_date,
            "headline": decision.summary, "decided_at": decision.decided_at,
            "review_of": decision.review_of, "superseded_by": decision.superseded_by,
            "items": evidence.get("items", []),
            "notes": evidence.get("notes", []),
            "impact": evidence.get("impact", []),
        }

    def decision_versions(self, lot_id: str) -> list[dict]:
        return [
            {
                "decision_id": d.decision_id, "version": d.version, "result": d.result,
                "result_cn": _RESULT_CN.get(d.result, d.result),
                "target_date": d.target_date, "decided_at": d.decided_at,
                "review_of": d.review_of, "superseded_by": d.superseded_by,
                "headline": d.summary,
            }
            for d in self.store.list_decisions(lot_id)
        ]

    def conflicts(self, lot_id: str = "") -> list[dict]:
        records = self.store.list_conflicts()
        if lot_id:
            records = [r for r in records if r.lot_id == lot_id]
        return [{
            "conflict_id": r.conflict_id, "lot_id": r.lot_id, "kind": r.kind,
            "detail": json.loads(r.detail), "created_at": r.created_at,
            "resolved_by": r.resolved_by,
        } for r in records]

    def applicable_rules(self, category: str, as_of: str) -> dict:
        policy = self.store.policy_for_category(category)
        engine = Engine(self._rulebook())
        view = engine._view(category, as_of, policy)
        return {
            "category": category, "as_of": as_of,
            "notes": list(view.notes),
            "limits": [
                {
                    "clause_id": r.clause.clause_id, "clause_no": r.clause.clause_no,
                    "package_id": r.provenance.package_id, "version": r.provenance.version,
                    "indicator": r.clause.indicator, "min_value": r.clause.min_value,
                    "max_value": r.clause.max_value, "unit": r.clause.unit,
                    "basis": r.clause.basis, "method_ref": r.clause.method_ref,
                    "effective_date": r.provenance.package_effective_date,
                    "trail": list(r.provenance.trail),
                }
                for r in view.limits
            ],
            "labels": [
                {
                    "clause_id": r.clause.clause_id, "clause_no": r.clause.clause_no,
                    "package_id": r.provenance.package_id, "version": r.provenance.version,
                    "label_code": r.clause.label_code, "label_required": r.clause.label_required,
                    "effective_date": r.provenance.package_effective_date,
                    "trail": list(r.provenance.trail),
                }
                for r in view.labels
            ],
        }

    # -- 批量迁移（可中断、可续跑） ----------------------------------------

    def migrate(self, campaign_id: str, lot_ids: list[str], target_date: str,
                limit: int | None = None) -> dict:
        """对一批产品执行判定/复核。

        检查点为 (campaign_id, lot_id)：已 done 的批次永远跳过，不重复生成决定；
        conflict 的批次在补料或解决漂移后的下一次调用中自动重试。
        limit 控制每轮处理量，处理完即返回，剩余批次下轮继续。
        """
        remaining_all = self.store.migration_pending(campaign_id, lot_ids)
        # 未尝试的批次优先；conflict/blocked 的批次排到一轮扫完之后重试，
        # 避免一个冲突批次在 --limit 下造成队头阻塞、剩余产品永远轮不到。
        attempted = {row["lot_id"] for row in self.store.list_migration(campaign_id)}
        remaining = sorted(remaining_all, key=lambda lot_id: (lot_id in attempted, lot_ids.index(lot_id)))
        planned = remaining[:limit] if limit else remaining
        stamp = now_stamp()
        items: list[dict] = []

        for lot_id in planned:
            batch = self.store.get_batch(lot_id)
            if batch is None:
                self.store.upsert_migration_item(
                    campaign_id, lot_id, MigrationStatus.CONFLICT, "", stamp
                )
                items.append({"lot_id": lot_id, "status": MigrationStatus.CONFLICT,
                              "kind": "not_found", "message": "批次不存在"})
                continue
            try:
                outcome = (
                    self.review(lot_id, target_date)
                    if batch.flow_state != FlowState.OPEN
                    else self.judge(lot_id, target_date)
                )
            except ServiceError as exc:
                self.store.upsert_migration_item(
                    campaign_id, lot_id, MigrationStatus.CONFLICT, "", stamp
                )
                items.append({"lot_id": lot_id, "status": MigrationStatus.CONFLICT,
                              "kind": exc.kind, "message": str(exc)})
                continue
            if outcome["status"] == DecisionResult.STOPPED:
                if outcome["kind"] == "missing_test":
                    checkpoint = MigrationStatus.BLOCKED
                else:
                    checkpoint = MigrationStatus.CONFLICT
                self.store.upsert_migration_item(
                    campaign_id, lot_id, checkpoint, "", stamp
                )
                items.append({"lot_id": lot_id, "status": checkpoint,
                              "kind": outcome["kind"],
                              **({"conflict_id": outcome["conflict_id"]} if outcome.get("conflict_id") else {}),
                              "message": outcome["message"]})
                continue
            self.store.upsert_migration_item(
                campaign_id, lot_id, MigrationStatus.DONE, outcome["decision_id"], stamp
            )
            items.append({
                "lot_id": lot_id, "status": MigrationStatus.DONE,
                "decision_id": outcome["decision_id"], "version": outcome["version"],
                "result": outcome["status"], "result_cn": outcome["result_cn"],
            })

        still_remaining = self.store.migration_pending(campaign_id, lot_ids)
        counts = Counter(i["status"] for i in items)
        return {
            "campaign_id": campaign_id, "target_date": target_date,
            "processed": len(items), "done": counts.get(MigrationStatus.DONE, 0),
            "conflict": counts.get(MigrationStatus.CONFLICT, 0),
            "blocked": counts.get(MigrationStatus.BLOCKED, 0),
            "remaining": len(still_remaining),
            "remaining_lots": still_remaining,
            "interruptible": True,
            "items": items,
        }

    def migration_status(self, campaign_id: str) -> list[dict]:
        return [dict(row) for row in self.store.list_migration(campaign_id)]
