import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from food_standard_transition.cli import main as cli_main
from food_standard_transition.domain import DecisionResult, FlowState
from food_standard_transition.service import Service, ServiceError
from food_standard_transition.store import Store

DATA = Path(__file__).resolve().parent.parent / "data"


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class 引擎测试基类(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service(Store())
        self.service.import_bundle(load("bundle_gb10765.json"))
        self.service.import_product(load("product_lot_a.json"))
        self.service.import_product(load("product_lot_b.json"))


class 双工厂时态判定测试(引擎测试基类):
    def test_旧厂库存_过渡期内判换标(self):
        outcome = self.service.judge("LOT-A-2022-11", "2024-06-01")
        self.assertEqual(outcome["status"], DecisionResult.RELABEL)
        # 限值仍按生产时旧规（库存新限值日 2025-02-22 未到）
        limit_items = [i for i in outcome["items"] if i["kind"] == "limit"]
        self.assertTrue(all(i["stage"] in ("production", "target") for i in limit_items))
        vd_items = [i for i in limit_items if i["indicator"] == "维生素D"]
        self.assertTrue(all(i["bases"][0]["version"] == "2010" for i in vd_items))

    def test_旧厂库存_标签宽限期满判停止流转(self):
        outcome = self.service.judge("LOT-A-2022-11", "2025-09-01")
        self.assertEqual(outcome["status"], DecisionResult.HALT)
        label_fail = [
            i for i in outcome["items"]
            if i["kind"] == "label" and i["stage"] == "target" and i["status"] == "fail"
        ]
        self.assertEqual({i["label_code"] for i in label_fail}, {"LBL_2021_NOTICE"})

    def test_旧厂库存_新限值生效后iu换算到新单位仍合规但需换标(self):
        outcome = self.service.judge("LOT-A-2022-11", "2025-06-01")
        self.assertEqual(outcome["status"], DecisionResult.RELABEL)
        target_vd = next(
            i for i in outcome["items"]
            if i["kind"] == "limit" and i["stage"] == "target"
            and i["indicator"] == "维生素D"
        )
        self.assertEqual(target_vd["aligned_unit"], "ug/100kJ")
        self.assertAlmostEqual(target_vd["aligned_value"], 0.3585, places=3)
        self.assertTrue(any("IU 换算" in s for s in target_vd["conversion_steps"]))
        self.assertEqual(target_vd["test"]["method_id"], "M-VD-2010")
        self.assertIn("M-VD-2021", target_vd["bases"][0]["method_ref"])

    def test_新厂批次_新国标下直接合规(self):
        outcome = self.service.judge("LOT-B-2023-05", "2024-06-01")
        self.assertEqual(outcome["status"], DecisionResult.COMPLIANT)
        target_vd = next(
            i for i in outcome["items"]
            if i["kind"] == "limit" and i["stage"] == "target"
            and i["indicator"] == "维生素D"
        )
        # 第1号修改单（2024-03-01）已把 3.4 上限从 0.6 放到 0.66
        self.assertEqual(target_vd["bases"][0]["clause_id"], "GB10765-2021-3.4-A1")
        self.assertTrue(any("第1号修改单" in t for t in target_vd["bases"][0]["trail"]))

    def test_生产时早于修改单_使用未修改条款(self):
        rules = self.service.applicable_rules("婴儿配方食品", "2023-06-01")
        vd = next(r for r in rules["limits"] if r["indicator"] == "维生素D")
        self.assertEqual(vd["clause_id"], "GB10765-2021-3.4")
        codes = {r["clause_id"] for r in rules["limits"]} | {l["clause_id"] for l in rules["labels"]}
        self.assertIn("GB10765-2021-5.3", codes)  # 5.3 尚未被废止

    def test_修改单只替换明确条款_其他不动(self):
        rules = self.service.applicable_rules("婴儿配方食品", "2024-06-01")
        ids = {r["clause_id"] for r in rules["limits"]}
        self.assertEqual(ids, {"GB10765-2021-3.1", "GB10765-2021-3.4-A1"})
        label_ids = {l["clause_id"] for l in rules["labels"]}
        self.assertEqual(label_ids, {"GB10765-2021-5.2"})  # 5.3 被明确废止


class 矛盾依据停止测试(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service(Store())
        self.service.import_bundle(load("bundle_conflict.json"))
        self.service.import_product(load("product_lot_conflict.json"))

    def test_互斥限值区间_停止判定并留冲突_不出结论(self):
        outcome = self.service.judge("LOT-C-SUPP-01", "2024-06-01")
        self.assertEqual(outcome["status"], DecisionResult.STOPPED)
        self.assertEqual(outcome["kind"], "contradiction")
        self.assertIsNone(self.service.decision("LOT-C-SUPP-01"))
        conflicts = self.service.conflicts("LOT-C-SUPP-01")
        self.assertEqual(len(conflicts), 1)
        intervals = conflicts[0]["detail"]["intervals_mg_per_100g"]
        self.assertEqual({i["clause_id"] for i in intervals},
                         {"GB22570-2014-3.2", "GB99999-2022-4.1"})

    def test_同一冲突重复判定不重复留档(self):
        first = self.service.judge("LOT-C-SUPP-01", "2024-06-01")
        second = self.service.judge("LOT-C-SUPP-01", "2024-07-01")
        self.assertEqual(first["conflict_id"], second["conflict_id"])
        self.assertEqual(len(self.service.conflicts("LOT-C-SUPP-01")), 1)


class 导入幂等与漂移测试(引擎测试基类):
    def test_重复导入不产生新版本(self):
        again = self.service.import_bundle(load("bundle_gb10765.json"))
        self.assertTrue(all(p["status"] == "identical" for p in again["packages"]))
        self.assertEqual(len(self.service.store.list_packages()), 2)

    def test_同编号不同内容锁定依赖计算(self):
        altered = load("bundle_gb10765.json")
        new_pkg = next(p for p in altered["packages"] if p["version"] == "2021")
        new_pkg["clauses"][0]["min_value"] = 0.40  # 篡改 3.1 下限
        result = self.service.import_bundle(altered)
        entry = next(p for p in result["packages"] if p["version"] == "2021")
        self.assertEqual(entry["status"], "drift_locked")

        # 依赖该标准的判定一律停止
        outcome = self.service.judge("LOT-B-2023-05", "2024-06-01")
        self.assertEqual(outcome["status"], DecisionResult.STOPPED)
        self.assertEqual(outcome["kind"], "drift_lock")

        # 再次提交同一篡改内容：仍是同一条漂移，不新增
        self.service.import_bundle(altered)
        self.assertEqual(len(self.service.list_drifts("locked")), 1)

        # 处置解除后判定恢复
        drift_id = entry["drift_id"]
        self.service.resolve_drift(drift_id, "确认以首次入库文本为准", "manager-wang")
        outcome2 = self.service.judge("LOT-B-2023-05", "2024-06-01")
        self.assertEqual(outcome2["status"], DecisionResult.COMPLIANT)
        conflict = self.service.conflicts("LOT-B-2023-05")[0]
        self.assertTrue(conflict["resolved_by"])


class 追加结论与复核测试(引擎测试基类):
    def test_已出具结论不得原地覆盖_只能复核新版本(self):
        v1 = self.service.judge("LOT-A-2022-11", "2024-06-01")
        self.assertEqual(v1["version"], 1)
        with self.assertRaises(ServiceError) as ctx:
            self.service.judge("LOT-A-2022-11", "2025-09-01")
        self.assertEqual(ctx.exception.kind, "flow_closed")

        v2 = self.service.review("LOT-A-2022-11", "2025-09-01")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["review_of"], v1["decision_id"])
        self.assertTrue(v2["result_changed"])

        versions = self.service.decision_versions("LOT-A-2022-11")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["superseded_by"], v2["decision_id"])
        # v1 内容原样保留
        self.assertEqual(versions[0]["result"], DecisionResult.RELABEL)

    def test_复核影响分析_标出修改单带来的变化(self):
        self.service.judge("LOT-B-2023-05", "2023-06-01")
        review = self.service.review("LOT-B-2023-05", "2024-06-01")
        self.assertFalse(review["result_changed"])
        changed = {tuple(c["item"]): c["change"] for c in review["impact"]}
        vd_key = ("target", "维生素D", "limit")
        self.assertEqual(changed.get(vd_key), "basis_changed")
        self.assertIn(("target", "LBL_FORBIDDEN_MARK", "label"),
                      {tuple(c["item"]) for c in review["impact"]})

    def test_复核遇冲突时原结论保留(self):
        self.service.judge("LOT-B-2023-05", "2023-06-01")
        altered = load("bundle_gb10765.json")
        next(p for p in altered["packages"] if p["version"] == "2021")["clauses"][0]["min_value"] = 0.40
        self.service.import_bundle(altered)
        stopped = self.service.review("LOT-B-2023-05", "2024-06-01")
        self.assertEqual(stopped["status"], DecisionResult.STOPPED)
        versions = self.service.decision_versions("LOT-B-2023-05")
        self.assertEqual(len(versions), 1)  # 未产生新版本
        self.assertEqual(versions[0]["result"], DecisionResult.COMPLIANT)


class 后补检测测试(引擎测试基类):
    def _lot_missing_vd(self) -> str:
        payload = load("product_lot_b.json")
        payload["batch"]["lot_id"] = "LOT-B-NOVD"
        payload["tests"] = [
            {**t, "test_id": f"{t['test_id']}-NOVD"}
            for t in payload["tests"] if t["indicator"] != "维生素D"
        ]
        self.service.import_product(payload)
        return "LOT-B-NOVD"

    def test_缺检测停止且不落冲突_补料后通过(self):
        lot_id = self._lot_missing_vd()
        stopped = self.service.judge(lot_id, "2024-06-01")
        self.assertEqual(stopped["status"], DecisionResult.STOPPED)
        self.assertEqual(stopped["kind"], "missing_test")
        self.assertFalse(stopped["persisted"])
        self.assertEqual(self.service.conflicts(lot_id), [])

        self.service.add_test({
            "test_id": "T-VD-LATE", "lot_id": lot_id, "indicator": "维生素D",
            "value": 0.45, "unit": "ug/100kJ", "method_id": "M-VD-2021",
            "tested_at": "2024-05-20", "received_at": "2024-05-25",
        })
        outcome = self.service.judge(lot_id, "2024-06-01")
        self.assertEqual(outcome["status"], DecisionResult.COMPLIANT)

    def test_流程结束后拒收后补检测(self):
        self.service.judge("LOT-B-2023-05", "2024-06-01")
        with self.assertRaises(ServiceError) as ctx:
            self.service.add_test({
                "test_id": "T-X", "lot_id": "LOT-B-2023-05", "indicator": "铁",
                "value": 1, "unit": "mg/100g", "method_id": "M-FE",
                "tested_at": "2024-07-01",
            })
        self.assertEqual(ctx.exception.kind, "flow_closed")
        batch = self.service.store.get_batch("LOT-B-2023-05")
        self.assertEqual(batch.flow_state, FlowState.DECIDED)


class 豁免职责分离测试(引擎测试基类):
    def test_起草人不得自行批准(self):
        draft = self.service.draft_exemption(load("exemption_lot_a.json"))
        with self.assertRaises(ServiceError) as ctx:
            self.service.decide_exemption(draft["exemption_id"], "regulator-li", True)
        self.assertEqual(ctx.exception.kind, "segregation_of_duties")

    def test_他人批准后豁免生效(self):
        draft = self.service.draft_exemption(load("exemption_lot_a.json"))
        self.service.decide_exemption(draft["exemption_id"], "director-zhao", True)
        # 2025-09 宽限期满：标签本应 halt，豁免针对新提示语条款 → 仅剩宽限判定不适用
        outcome = self.service.judge("LOT-A-2022-11", "2025-09-01")
        exempt_items = [i for i in outcome["items"] if i.get("status") == "exempt"]
        self.assertTrue(any(i["label_code"] == "LBL_2021_NOTICE" for i in exempt_items
                            if i["kind"] == "label"))
        self.assertEqual(outcome["status"], DecisionResult.COMPLIANT)


class 批量迁移测试(引擎测试基类):
    def setUp(self) -> None:
        super().setUp()
        self.service.import_bundle(load("bundle_conflict.json"))
        self.service.import_product(load("product_lot_conflict.json"))

    def test_中断后续跑且不重复生成决定(self):
        lots = ["LOT-A-2022-11", "LOT-B-2023-05", "LOT-C-SUPP-01"]

        first = self.service.migrate("CMP-001", lots, "2024-06-01", limit=1)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(first["done"], 1)
        self.assertEqual(first["remaining"], 2)

        second = self.service.migrate("CMP-001", lots, "2024-06-01", limit=1)
        self.assertEqual(second["items"][0]["lot_id"], "LOT-B-2023-05")
        third = self.service.migrate("CMP-001", lots, "2024-06-01")
        self.assertEqual(third["done"], 0)  # 只剩冲突批次
        self.assertEqual(third["conflict"], 1)

        # 整体重跑：已完成批次被检查点跳过，不生成新版本；未解决的冲突批次会重试但仍停止
        again = self.service.migrate("CMP-001", lots, "2024-06-01")
        self.assertEqual(again["done"], 0)
        self.assertEqual(again["conflict"], 1)
        self.assertEqual(len(self.service.decision_versions("LOT-A-2022-11")), 1)
        self.assertEqual(len(self.service.decision_versions("LOT-B-2023-05")), 1)

        checkpoint = {row["lot_id"]: row["status"]
                      for row in self.service.migration_status("CMP-001")}
        self.assertEqual(checkpoint["LOT-C-SUPP-01"], "conflict")

    def test_冲突批次不得在limit下队头阻塞剩余产品(self):
        # 冲突批次排在最前：首轮撞到冲突，次轮必须越过它处理后续产品
        lots = ["LOT-C-SUPP-01", "LOT-A-2022-11", "LOT-B-2023-05"]
        first = self.service.migrate("CMP-003", lots, "2024-06-01", limit=1)
        self.assertEqual(first["items"][0]["lot_id"], "LOT-C-SUPP-01")
        self.assertEqual(first["items"][0]["status"], "conflict")
        second = self.service.migrate("CMP-003", lots, "2024-06-01", limit=1)
        self.assertEqual(second["items"][0]["lot_id"], "LOT-A-2022-11")
        self.assertEqual(second["done"], 1)
        third = self.service.migrate("CMP-003", lots, "2024-06-01", limit=1)
        self.assertEqual(third["items"][0]["lot_id"], "LOT-B-2023-05")
        self.assertEqual(third["done"], 1)

    def test_缺检测批次blocked_补料后下一轮完成(self):
        payload = load("product_lot_b.json")
        payload["batch"]["lot_id"] = "LOT-B-PENDING"
        payload["tests"] = [
            {**t, "test_id": f"{t['test_id']}-P"}
            for t in payload["tests"] if t["indicator"] != "维生素D"
        ]
        self.service.import_product(payload)

        run = self.service.migrate(
            "CMP-002", ["LOT-B-PENDING", "LOT-B-2023-05"], "2024-06-01"
        )
        self.assertEqual(run["blocked"], 1)
        self.assertEqual(run["done"], 1)

        self.service.add_test({
            "test_id": "T-VD-P", "lot_id": "LOT-B-PENDING", "indicator": "维生素D",
            "value": 0.45, "unit": "ug/100kJ", "method_id": "M-VD-2021",
            "tested_at": "2024-05-20", "received_at": "2024-05-25",
        })
        rerun = self.service.migrate(
            "CMP-002", ["LOT-B-PENDING", "LOT-B-2023-05"], "2024-06-01"
        )
        self.assertEqual(rerun["done"], 1)
        self.assertEqual(rerun["remaining"], 0)


class 命令行测试(unittest.TestCase):
    def test_cli_完整链路与退出码(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "fst.db")

            def run(*argv: str) -> tuple[int, str]:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = cli_main(["--db", db, *argv])
                return code, buf.getvalue()

            code, out = run("import", str(DATA / "bundle_gb10765.json"))
            self.assertEqual(code, 0)
            self.assertIn("imported", out)

            code, out = run("product", str(DATA / "product_lot_b.json"))
            self.assertEqual(code, 0)

            code, out = run("judge", "--lot", "LOT-B-2023-05", "--date", "2024-06-01")
            self.assertEqual(code, 0)
            self.assertIn('"status": "compliant"', out)

            code, out = run("decision", "--lot", "LOT-B-2023-05", "--items")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["items"])
            self.assertTrue(any("IU" in s or "方法" in s or "换算" in s
                                for i in payload["items"]
                                for s in i.get("conversion_steps", [])))


if __name__ == "__main__":
    unittest.main()
