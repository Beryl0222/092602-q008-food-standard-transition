"""时态规则解析：按事件时点选取有效标准，叠加修改单，识别矛盾依据与漂移锁定。

解析器只处理"某时点适用哪些条款"，不接触检测数值；跨标准的限值区间是否互斥
需要单位换算后才能判断，因此放在判定引擎中。
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain import (
    Amendment,
    Clause,
    Drift,
    DriftStatus,
    Method,
    MethodEquivalence,
    StandardPackage,
    TransitionPolicy,
)


class RuleResolutionError(Exception):
    """无法形成唯一、可计算的规则集：无依据、漂移锁定等。"""

    def __init__(self, kind: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail or {}


@dataclass(frozen=True)
class Provenance:
    """条款时点证据：来自哪版标准、经过哪些修改单。"""
    package_id: str
    version: str
    package_title: str
    clause_no: str
    package_effective_date: str
    trail: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedLimit:
    clause: Clause
    provenance: Provenance


@dataclass(frozen=True)
class ResolvedLabel:
    clause: Clause
    provenance: Provenance


@dataclass(frozen=True)
class RuleView:
    """某一事件时点、某一品类的有效规则集。"""
    as_of: str
    category: str
    limits: tuple[ResolvedLimit, ...] = ()
    labels: tuple[ResolvedLabel, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OverlayEntry:
    clause: Clause | None  # None 表示废止
    trail: tuple[str, ...]


class RuleBook:
    """不可变规则库快照，由存储层装配，判定期间不变化。"""

    def __init__(
        self,
        packages: list[StandardPackage],
        clauses: list[Clause],
        amendments: list[Amendment],
        policies: list[TransitionPolicy],
        methods: list[Method],
        equivalences: list[MethodEquivalence],
        drifts: list[Drift],
    ) -> None:
        self.packages = {(p.package_id, p.version): p for p in packages}
        self.clauses: dict[tuple[str, str], list[Clause]] = {}
        for clause in clauses:
            self.clauses.setdefault((clause.package_id, clause.version), []).append(clause)
        self.amendments = sorted(amendments, key=lambda a: (a.effective_date, a.number))
        self.policies = policies
        self.methods = {m.method_id: m for m in methods}
        self.equivalences = equivalences
        self.drifts = drifts

    # -- 检验方法版本 ------------------------------------------------------

    def methods_comparable(self, test_method_id: str, required_method_id: str, indicator: str) -> bool:
        if test_method_id == required_method_id:
            return True
        for edge in self.equivalences:
            pair = {edge.method_id, edge.other_method_id}
            if pair == {test_method_id, required_method_id} and edge.indicator == indicator:
                return edge.comparable
        return False  # 未显式声明可比，即不可比，禁止默认放行

    # -- 漂移 --------------------------------------------------------------

    def assert_no_locked_drift(self, keys: list[tuple[str, str]]) -> None:
        locked = [
            d for d in self.drifts
            if d.status == DriftStatus.LOCKED and (d.package_id, d.version) in keys
        ]
        if locked:
            d = locked[0]
            raise RuleResolutionError(
                "drift_lock",
                f"标准 {d.package_id} {d.version} 存在同编号不同内容的导入，依赖计算已锁定",
                {
                    "package_id": d.package_id,
                    "version": d.version,
                    "existing_fingerprint": d.existing_fingerprint,
                    "incoming_fingerprint": d.incoming_fingerprint,
                    "detected_at": d.detected_at,
                },
            )

    # -- 内部：基础条款 + 修改单叠加 ---------------------------------------

    def _overlay(self, package_id: str, version: str, category: str, as_of: str) -> dict[str, _OverlayEntry]:
        result: dict[str, _OverlayEntry] = {}
        for clause in self.clauses.get((package_id, version), []):
            if not clause.categories or category in clause.categories:
                result[clause.clause_no] = _OverlayEntry(
                    clause, (f"{clause.clause_no} 基础条款，自版本 {version} 起有效",)
                )
        for amendment in self.amendments:
            if amendment.package_id != package_id or amendment.version != version:
                continue
            if amendment.effective_date > as_of:
                continue  # 事件时点尚未发布的修改单不生效
            for replacement in amendment.replacements:
                if replacement.clause is not None and replacement.clause.categories and category not in replacement.clause.categories:
                    continue
                trail_tail = (
                    f"由修改单 {amendment.number}（{amendment.effective_date} 生效）"
                    + ("整条替换" if replacement.action == "replace" else "废止")
                    + f"：{amendment.summary}",
                )
                if replacement.action == "repeal":
                    previous = result.get(replacement.clause_no)
                    base_trail = previous.trail if previous else ("该条款此前不存在",)
                    result[replacement.clause_no] = _OverlayEntry(None, base_trail + trail_tail)
                else:
                    new_clause = replacement.clause
                    previous = result.get(replacement.clause_no)
                    base_trail = previous.trail if previous else (f"{replacement.clause_no} 由修改单新增",)
                    result[replacement.clause_no] = _OverlayEntry(new_clause, base_trail + trail_tail)
        return result

    def _to_view_entries(
        self, package_id: str, version: str, category: str, as_of: str
    ) -> tuple[list[ResolvedLimit], list[ResolvedLabel], tuple[str, ...]]:
        package = self.packages[(package_id, version)]
        overlay = self._overlay(package_id, version, category, as_of)
        limits: list[ResolvedLimit] = []
        labels: list[ResolvedLabel] = []
        for clause_no in sorted(overlay):
            entry = overlay[clause_no]
            if entry.clause is None:
                continue  # 已被修改单明确废止
            provenance = Provenance(
                package_id=package_id,
                version=version,
                package_title=package.title,
                clause_no=clause_no,
                package_effective_date=package.effective_date,
                trail=entry.trail,
            )
            if entry.clause.kind == "label":
                labels.append(ResolvedLabel(entry.clause, provenance))
            else:
                limits.append(ResolvedLimit(entry.clause, provenance))
        return limits, labels, ()

    # -- 内部：包的有效性与谱系 --------------------------------------------

    def _active(self, package: StandardPackage, as_of: str) -> bool:
        if package.effective_date > as_of:
            return False
        if package.repeal_date and package.repeal_date <= as_of:
            return False
        return True

    def _policy_for(self, category: str) -> TransitionPolicy | None:
        matches = [p for p in self.policies if p.category == category]
        return matches[0] if matches else None

    def _governing_key(
        self, category: str, as_of: str, switch_date: str
    ) -> tuple[tuple[str, str], list[str]]:
        """在新旧标准谱系内按切换日期选择治理版本。"""
        policy = self._policy_for(category)
        if policy is None:
            raise RuleResolutionError(
                "no_policy", f"品类 {category} 缺少新旧标准过渡政策，无法确定谱系", {"category": category}
            )
        old_key = (policy.old_package_id, policy.old_version)
        new_key = (policy.new_package_id, policy.new_version)
        old_pkg = self.packages.get(old_key)
        new_pkg = self.packages.get(new_key)
        notes: list[str] = [
            f"过渡政策：新标准 {policy.new_package_id} {policy.new_version} 于 {policy.new_effective_date} 生效，"
            f"强制切换日 {policy.enforcement_date}，旧标签可使用至 {policy.old_label_use_until}"
        ]
        if old_pkg is None or not self._active(old_pkg, as_of):
            notes.append(f"{as_of} 时旧标准已失效，适用 {policy.new_package_id} {policy.new_version}")
            return new_key, notes
        if new_pkg is None or not self._active(new_pkg, as_of):
            notes.append(f"{as_of} 时新标准尚未生效，适用 {policy.old_package_id} {policy.old_version}")
            return old_key, notes
        chosen = old_key if as_of < switch_date else new_key
        notes.append(
            f"{as_of} 与切换日 {switch_date} 比较，适用 "
            f"{chosen[0]} {chosen[1]}"
        )
        return chosen, notes

    # -- 对外视图 ----------------------------------------------------------

    def lineage_view(self, category: str, as_of: str, switch_date: str) -> RuleView:
        """存在过渡政策的品类：沿新旧谱系取单一治理版本。"""
        (package_id, version), notes = self._governing_key(category, as_of, switch_date)
        self.assert_no_locked_drift([(package_id, version)])
        limits, labels, _ = self._to_view_entries(package_id, version, category, as_of)
        return RuleView(as_of=as_of, category=category, limits=tuple(limits), labels=tuple(labels), notes=tuple(notes))

    def active_view(self, category: str, as_of: str) -> RuleView:
        """无过渡政策的品类：所有现行有效标准同时适用，限值在引擎中取交集。"""
        active = [
            p for p in self.packages.values()
            if self._active(p, as_of) and (not p.categories or category in p.categories)
        ]
        if not active:
            raise RuleResolutionError(
                "no_basis", f"{as_of} 时品类 {category} 没有任何有效标准", {"category": category, "as_of": as_of}
            )
        self.assert_no_locked_drift([(p.package_id, p.version) for p in active])
        limits: list[ResolvedLimit] = []
        labels: list[ResolvedLabel] = []
        notes: list[str] = []
        for package in sorted(active, key=lambda p: (p.package_id, p.version)):
            pl, lb, _ = self._to_view_entries(package.package_id, package.version, category, as_of)
            limits.extend(pl)
            labels.extend(lb)
            notes.append(f"现行有效标准并列适用：{package.package_id} {package.version}《{package.title}》")
        return RuleView(as_of=as_of, category=category, limits=tuple(limits), labels=tuple(labels), notes=tuple(notes))
