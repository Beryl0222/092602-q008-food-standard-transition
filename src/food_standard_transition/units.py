"""指标单位与判定基数换算。

支持三类换算，全部为显式、可留痕的换算，不做任何隐式假设：

1. 质量分数单位互换（g/100g、mg/kg、µg/100g 等）；
2. 能量密度基数与质量基数互换（每 100kcal / 每 100kJ ↔ 每 100g），
   需要配方快照中的能量密度；
3. 维生素 IU 与质量单位互换，换算因子按指标显式登记，未知指标拒绝猜测。
"""
from __future__ import annotations

from dataclasses import dataclass


KCAL_TO_KJ = 4.184

# 质量单位 → 毫克
_MASS_TO_MG = {
    "g": 1000.0,
    "mg": 1.0,
    "ug": 0.001,
    "µg": 0.001,
}

# 1 IU 对应的质量（质量单位, 质量量），按指标显式登记
IU_FACTORS: dict[str, tuple[str, float]] = {
    "维生素A": ("ug", 0.3),        # 1 IU = 0.3 µg 视黄醇当量
    "维生素D": ("ug", 0.025),      # 1 IU = 0.025 µg 胆钙化醇
    "维生素E": ("mg", 0.67),       # 1 IU = 0.67 mg d-α-生育酚
}

_ENERGY_DENOMS = {"100kcal", "100kj"}


class UnitConversionError(ValueError):
    """单位不可识别、缺换算依据或基数不兼容。"""


@dataclass(frozen=True)
class Alignment:
    value: float
    unit: str
    steps: tuple[str, ...]


def _normalize_token(token: str) -> str:
    return token.strip().replace("μ", "µ").lower()


def _parse_unit(unit: str) -> tuple[str, str]:
    """拆成 (质量单位, 分母基数)，如 mg/100g -> ('mg','100g')。"""
    token = _normalize_token(unit)
    if "/" not in token:
        raise UnitConversionError(f"无法识别的单位（缺少判定基数）：{unit}")
    mass, denom = token.split("/", 1)
    if mass not in _MASS_TO_MG:
        raise UnitConversionError(f"不支持的质量单位：{mass}（来自 {unit}）")
    if denom not in ("100g", "kg") and denom not in _ENERGY_DENOMS:
        raise UnitConversionError(f"不支持的判定基数：{denom}（来自 {unit}）")
    return mass, denom


def _iu_to_mass(unit: str, value: float, indicator: str) -> tuple[float, str, list[str]]:
    """把 IU/<基数> 的数值按指标换算成质量单位，返回新数值、新单位与留痕步骤。"""
    token = _normalize_token(unit)
    mass, denom = token.split("/", 1)
    if mass != "iu":
        return value, unit, []
    factor = IU_FACTORS.get(indicator)
    if factor is None:
        raise UnitConversionError(f"指标 {indicator} 未登记 IU 换算因子，禁止猜测")
    mass_unit, amount = factor
    return (
        value * amount,
        f"{mass_unit}/{denom}",
        [f"IU 换算：{value:g} IU × {amount:g} = {value * amount:g} {mass_unit}（{indicator}）"],
    )


def align_to_clause(
    value: float,
    unit: str,
    target_unit: str,
    *,
    indicator: str,
    energy_kcal_per_100g: float | None = None,
) -> Alignment:
    """把检测值换算到条款声明的单位与基数，返回换算后的值与逐步留痕。"""
    steps: list[str] = []

    # 0) 检测单位与条款单位完全一致：无需换算（IU/IU 同单位比较同样放行）
    if _normalize_token(unit) == _normalize_token(target_unit):
        return Alignment(value=value, unit=target_unit, steps=("检测单位与条款单位一致，无需换算",))

    # 1) IU 先行转成质量单位（数值同步换算）
    if _normalize_token(unit).split("/", 1)[0] == "iu":
        value, unit, iu_steps = _iu_to_mass(unit, value, indicator)
        steps.extend(iu_steps)
    if _normalize_token(target_unit).split("/", 1)[0] == "iu":
        raise UnitConversionError("限值条款以 IU 表述时，请在条款中改用质量单位后再判定")

    from_mass, from_denom = _parse_unit(unit)
    to_mass, to_denom = _parse_unit(target_unit)

    # 2) 统一到 mg / 100g
    per_100g = value * _MASS_TO_MG[from_mass]
    steps.append(f"质量单位换算：{value:g} {unit} → {per_100g:g} mg/{from_denom}")

    energy_involved = from_denom in _ENERGY_DENOMS or to_denom in _ENERGY_DENOMS
    if energy_involved:
        if not energy_kcal_per_100g:
            raise UnitConversionError("能量基数换算需要配方能量密度，但批次缺少配方快照")

    def denom_to_100g(amount_per_denom: float, denom: str) -> float:
        if denom == "100g":
            return amount_per_denom
        if denom == "kg":
            return amount_per_denom / 10.0
        if denom == "100kcal":
            return amount_per_denom * energy_kcal_per_100g / 100.0
        # 100kJ
        return amount_per_denom * energy_kcal_per_100g * KCAL_TO_KJ / 100.0

    per_mg_100g = denom_to_100g(per_100g, from_denom)
    if from_denom != "100g":
        steps.append(
            f"基数换算：按配方能量 {energy_kcal_per_100g:g} kcal/100g 折为 {per_mg_100g:g} mg/100g"
            if from_denom in _ENERGY_DENOMS
            else f"基数换算：mg/kg → mg/100g（÷10）得 {per_mg_100g:g} mg/100g"
        )

    # 3) 从 mg/100g 折到目标分母与目标质量单位
    if to_denom == "100g":
        target_amount = per_mg_100g
    elif to_denom == "kg":
        target_amount = per_mg_100g * 10.0
    elif to_denom == "100kcal":
        target_amount = per_mg_100g * 100.0 / energy_kcal_per_100g
    else:  # 100kJ
        target_amount = per_mg_100g * 100.0 / (energy_kcal_per_100g * KCAL_TO_KJ)

    result = target_amount / _MASS_TO_MG[to_mass]
    if to_denom != "100g" or to_mass != "mg":
        steps.append(f"折回条款单位：{per_mg_100g:g} mg/100g → {result:g} {target_unit}")

    return Alignment(value=result, unit=target_unit, steps=tuple(steps))
