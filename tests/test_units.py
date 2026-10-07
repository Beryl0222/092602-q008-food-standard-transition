import unittest

from food_standard_transition.units import (
    KCAL_TO_KJ,
    UnitConversionError,
    align_to_clause,
)


class 单位换算测试(unittest.TestCase):
    def test_相同单位无需换算(self):
        result = align_to_clause(0.55, "g/100kJ", "g/100kJ", indicator="蛋白质")
        self.assertEqual(result.value, 0.55)
        self.assertEqual(result.steps[0], "检测单位与条款单位一致，无需换算")

    def test_mg每kg与mg每100g(self):
        result = align_to_clause(20, "mg/kg", "mg/100g", indicator="铅")
        self.assertAlmostEqual(result.value, 2.0)  # 20 mg/kg = 2 mg/100g

    def test_每100kcal折每100g(self):
        # 1.5 µg/100kcal，能量 500 kcal/100g → 7.5 µg/100g
        result = align_to_clause(1.5, "ug/100kcal", "ug/100g",
                                 indicator="维生素D", energy_kcal_per_100g=500)
        self.assertAlmostEqual(result.value, 7.5)

    def test_每100g折每100kj(self):
        # 7.5 µg/100g ÷ (500*4.184/100) = 7.5/20.92 µg/100kJ
        result = align_to_clause(7.5, "ug/100g", "ug/100kJ",
                                 indicator="维生素D", energy_kcal_per_100g=500)
        self.assertAlmostEqual(result.value, 7.5 / (500 * KCAL_TO_KJ / 100))

    def test_iu维生素D跨单位(self):
        # 60 IU/100kcal × 0.025 µg/IU = 1.5 µg/100kcal → 7.5 µg/100g
        result = align_to_clause(60, "IU/100kcal", "ug/100g",
                                 indicator="维生素D", energy_kcal_per_100g=500)
        self.assertAlmostEqual(result.value, 7.5)
        self.assertTrue(any("IU 换算" in step for step in result.steps))

    def test_iu同单位直接比较(self):
        result = align_to_clause(60, "IU/100kcal", "IU/100kcal", indicator="维生素D")
        self.assertEqual(result.value, 60)

    def test_未知指标拒绝猜测iu因子(self):
        with self.assertRaises(UnitConversionError):
            align_to_clause(10, "IU/100g", "mg/100g", indicator="未知成分X")

    def test_能量基数缺配方快照即停止(self):
        with self.assertRaises(UnitConversionError):
            align_to_clause(1.5, "ug/100kcal", "ug/100g", indicator="维生素D")

    def test_不支持的单位(self):
        with self.assertRaises(UnitConversionError):
            align_to_clause(1, "粒/100g", "mg/100g", indicator="铁")


if __name__ == "__main__":
    unittest.main()
