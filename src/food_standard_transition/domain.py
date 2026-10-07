"""食品标准迁移判定引擎的领域记录。

所有业务日期使用 ISO ``YYYY-MM-DD`` 字符串；审计时间使用带时区的 ISO 时间戳。
记录均为不可变值对象，持久化与 JSON 转换通过 :meth:`to_dict` 完成。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 枚举（以字符串常量保存，避免 SQLite 中出现裸整数状态）
# ---------------------------------------------------------------------------

class ClauseKind:
    LIMIT = "limit"                 # 指标限值
    LABEL = "label"                 # 标签声明要求


class DecisionResult:
    COMPLIANT = "compliant"         # 合规
    RELABEL = "relabel"             # 换标
    HALT = "halt"                   # 停止流转
    STOPPED = "stopped"             # 矛盾/锁定，判定停止，未出具结论


class FlowState:
    OPEN = "open"                   # 流程未结束，可接收后补检测
    DECIDED = "decided"             # 已出具结论，检测通道关闭，只能复核


class DriftStatus:
    LOCKED = "locked"
    RESOLVED = "resolved"


class ConflictKind:
    BASIS = "contradiction"         # 互相矛盾的依据
    DRIFT = "drift_lock"            # 同编号不同内容导致的依赖锁定
    METHOD = "method_conflict"      # 强制性检验方法版本冲突
    UNIT = "unit_incompatible"      # 指标单位无换算依据


class MigrationStatus:
    PENDING = "pending"
    DONE = "done"
    CONFLICT = "conflict"           # 依据矛盾/锁定，解决后可继续重试
    BLOCKED = "blocked"             # 证据不全（如缺检测），补料后可继续重试


class ExemptionStatus:
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


# ---------------------------------------------------------------------------
# 兼容既有基线的记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int = 1
    created_at: str = ""

    def stamped(self) -> "Record":
        value = self.created_at or now_stamp()
        return replace(self, created_at=value)


# ---------------------------------------------------------------------------
# 规则侧
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StandardPackage:
    """规则包（标准）：按编号与版本存储，摘要用于识别与证据展示。"""
    package_id: str
    version: str
    title: str
    summary: str
    effective_date: str
    fingerprint: str
    imported_at: str = ""
    repeal_date: str = ""
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Clause:
    """标准条款。

    限值条款使用 ``min_value`` / ``max_value``（可只设其一），并声明计量单位、
    判定基数（每 100g / 每 100kcal / 每 100kJ）与强制检验方法版本。
    标签条款使用 ``label_code`` 与 ``label_required``（要求出现或禁止出现）。
    """
    clause_id: str
    package_id: str
    version: str
    clause_no: str
    title: str
    kind: str
    categories: tuple[str, ...] = ()
    indicator: str = ""
    basis: str = "per_100g"
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""
    method_ref: str = ""
    label_code: str = ""
    label_required: bool = True
    note: str = ""


@dataclass(frozen=True)
class Replacement:
    """修改单对单个条款的覆盖；``action=repeal`` 表示废止，``replace`` 表示整条替换。"""
    clause_no: str
    action: str
    clause: Clause | None = None


@dataclass(frozen=True)
class Amendment:
    """修改单：只替换其明确列出的条款。"""
    amendment_id: str
    package_id: str
    version: str
    number: str
    effective_date: str
    summary: str
    replacements: tuple[Replacement, ...] = ()


@dataclass(frozen=True)
class Method:
    """检验方法版本，如 GB 5009.5-2016（蛋白质测定）。"""
    method_id: str
    code: str
    version: str
    indicator: str
    title: str = ""
    issued_date: str = ""


@dataclass(frozen=True)
class MethodEquivalence:
    """两个方法版本是否就同一指标可比（需显式声明，不做隐式假设）。"""
    method_id: str
    other_method_id: str
    indicator: str
    comparable: bool
    note: str = ""


@dataclass(frozen=True)
class TransitionPolicy:
    """过渡政策：新旧标准衔接、旧标签库存期限与新指标对库存的生效时点。"""
    policy_id: str
    category: str
    old_package_id: str
    old_version: str
    new_package_id: str
    new_version: str
    new_effective_date: str
    enforcement_date: str
    old_label_use_until: str
    stock_new_limits_from: str = ""   # 空表示与 enforcement_date 相同
    note: str = ""


@dataclass(frozen=True)
class Drift:
    """同编号同版本但内容不同的导入：登记漂移并锁定依赖计算，原内容不被覆盖。"""
    drift_id: str
    package_id: str
    version: str
    existing_fingerprint: str
    incoming_fingerprint: str
    detected_at: str
    status: str = DriftStatus.LOCKED
    resolution: str = ""


# ---------------------------------------------------------------------------
# 产品/批次侧
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FormulaSnapshot:
    """配方快照：能量密度用于每 100g 与每能量基数之间的换算。"""
    snapshot_id: str
    lot_id: str
    energy_kcal_per_100g: float
    components: tuple[tuple[str, str], ...] = ()  # (指标, 单位) 声明性组成
    recorded_at: str = ""


@dataclass(frozen=True)
class LabelDeclaration:
    lot_id: str
    label_code: str
    present: bool
    label_version_date: str = ""


@dataclass(frozen=True)
class Batch:
    lot_id: str
    product_name: str
    category: str
    factory_id: str
    production_date: str
    snapshot_id: str = ""
    flow_state: str = FlowState.OPEN
    decided_at: str = ""


@dataclass(frozen=True)
class TestResult:
    """检验结果；``received_at`` 为进入流程的时间，晚于首次结论则拒收。"""
    test_id: str
    lot_id: str
    indicator: str
    value: float
    unit: str
    method_id: str
    tested_at: str
    received_at: str


@dataclass(frozen=True)
class Exemption:
    """豁免：起草人与批准人必须不同，只有已批准豁免可作为判定依据。"""
    exemption_id: str
    scope: str                  # lot / category
    scope_value: str
    clause_id: str
    reason: str
    drafted_by: str
    approved_by: str = ""
    status: str = ExemptionStatus.DRAFT
    created_at: str = ""
    decided_at: str = ""


# ---------------------------------------------------------------------------
# 结论侧（只追加）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    decision_id: str
    lot_id: str
    version: int
    result: str
    production_date: str
    target_date: str
    summary: str = ""           # JSON 文本
    evidence: str = ""          # JSON 文本：条款时点/换算/方法/标签依据
    decided_at: str = ""
    review_of: str = ""         # 上一版本 decision_id
    superseded_by: str = ""


@dataclass(frozen=True)
class ConflictRecord:
    """矛盾依据或漂移锁定：停止判定时原样保留，不出具任何结论。"""
    conflict_id: str
    lot_id: str
    kind: str
    detail: str                 # JSON 文本
    created_at: str
    resolved_by: str = ""       # 解决后补发的 decision_id


@dataclass(frozen=True)
class MigrationItem:
    campaign_id: str
    lot_id: str
    status: str
    decision_id: str = ""
    updated_at: str = ""
