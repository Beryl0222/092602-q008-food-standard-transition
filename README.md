# 食品标准迁移判定引擎

把食品标准条款及修改单的覆盖关系、适用品类、指标单位、检验方法版本、配方快照、
生产批次、标签声明、过渡政策与整改决定，保存为**可计算的历史**：引擎按每个批次
真实发生的时间选择规则，输出"合规 / 换标 / 停止流转"的明确结论，且每一项都能
回看使用的是哪版条款、做了什么单位换算、用的哪种检验方法，以及后续修改对原结论
的影响。

## 设计原则

- **按事件时点选规则**：生产时判定按生产日期取规则；目标日期判定按目标日期取规则。
  事件时点尚未生效的修改单不参与判定。
- **修改单只替换明确覆盖的条款**：按条款号 `replace` / `repeal`，其余条款原样保留，
  证据中保留完整覆盖轨迹。
- **矛盾依据即停止**：不同标准在同一时点对同一指标给出互斥限值（换算后区间无交集）、
  强制检验方法版本互不可比、或同编号规则内容漂移时，**停止判定、不出结论、保留冲突**。
  过渡政策中显式声明的新旧替代关系不视为矛盾。
- **结论只追加**：已出具的结论永不原地修改；新的事实（新标准、修改单、复核检测）通过
  `review` 生成新版本，并给出逐项影响分析。
- **后补检测只进入未结束流程**：批次 `open` 时可补检测；一旦 `decided`，检测通道关闭，
  更正只能走复核。
- **规则包按编号+摘要（内容指纹）识别**：重复导入不产生新版本；同编号同版本但内容不同
  会登记漂移并锁定依赖计算，原入库内容不被覆盖。
- **豁免职责分离**：起草人不得独自批准豁免，只有他人批准的豁免才能作为判定依据。
- **批量迁移可中断、可续跑**：检查点为 `(迁移批次号, 产品批次)`，已完成批次不重复生成
  决定；冲突/缺料批次延后重试，不会阻塞剩余产品。

## 目录

| 路径 | 说明 |
| --- | --- |
| `src/food_standard_transition/domain.py` | 领域记录（规则包、条款、修改单、批次、决定、冲突、豁免、迁移） |
| `src/food_standard_transition/units.py` | 单位与判定基数换算（质量单位、每100g↔每100kcal/100kJ、IU） |
| `src/food_standard_transition/rules.py` | 时态规则解析：有效标准、修改单叠加、漂移锁定、方法可比性 |
| `src/food_standard_transition/engine.py` | 判定引擎：限值/标签逐项计算、矛盾检测、复核影响分析 |
| `src/food_standard_transition/store.py` | SQLite 存储；结论、冲突、漂移为只追加表 |
| `src/food_standard_transition/service.py` | 应用服务：导入幂等、判定编排、复核、豁免、可恢复迁移 |
| `src/food_standard_transition/cli.py` | 命令行入口 |
| `contracts/record.json` | 输入契约（中文字段说明） |
| `data/` | 双工厂对照场景演示数据 |
| `tests/` | 33 个测试：单位换算、时态覆盖、矛盾停止、漂移、复核、豁免、迁移、CLI |

## 运行

```bash
# 测试
PYTHONPATH=src python3 -m unittest discover -s tests

# 语法检查
python3 -m compileall -q src tests

# 旧版冒烟入口仍然可用
PYTHONPATH=src python3 -m food_standard_transition.cli validate data/sample.json
```

数据库默认为当前目录 `fst.db`，可用 `--db` 或环境变量 `FST_DB` 指定。

## 场景走查：两个工厂为何结论相反

`data/` 中构造了同一配方婴儿奶粉在两个工厂的批次：

- `LOT-A-2022-11`：工厂 A，2022-11-15 生产，旧标签库存，检测按旧法、维生素 D 以 IU 报告；
- `LOT-B-2023-05`：工厂 B，2023-05-10 生产，新标签，检测按新法、维生素 D 以 µg 报告；
- 标准 GB 10765 有 2010 / 2021 两版，2023-02-22 切换；2021 版另有
  **2024-03-01 生效的第 1 号修改单**（调整维生素 D 上限、废止一条标签条款）；
- 过渡政策：库存 2025-02-22 起适用新限值，旧标签可用至 2025-08-22。

```bash
DB=demo.db
CLI="python3 -m food_standard_transition.cli --db $DB"
export PYTHONPATH=src

$CLI import data/bundle_gb10765.json            # 标准+修改单+方法+过渡政策
$CLI product data/product_lot_a.json data/product_lot_b.json

# 旧批次在过渡期内：限值按生产时旧规仍合规，但标签须更换 → relabel
$CLI judge --lot LOT-A-2022-11 --date 2024-06-01
# 新批次按新国标 + 已生效修改单 → compliant
$CLI judge --lot LOT-B-2023-05 --date 2024-06-01

# 逐项查看：条款时点、IU→µg 换算步骤、检验方法版本、修改单轨迹
$CLI decision --lot LOT-B-2023-05 --items
$CLI rules --category 婴儿配方食品 --date 2024-06-01

# 旧标签宽限期满后复核：原结论保留为 v1，生成 v2（halt）并给出影响分析
$CLI review --lot LOT-A-2022-11 --date 2025-09-01
$CLI versions --lot LOT-A-2022-11
```

复核 v2 的维生素 D 证据会展示类似：

```text
对齐值: 0.3585 ug/100kJ
检测方法: M-VD-2010   条款方法: M-VD-2021
条款: GB10765-2021-3.4-A1
  IU 换算：60 IU × 0.025 = 1.5 ug（维生素D）
  质量单位换算：1.5 ug/100kcal → 0.0015 mg/100kcal
  基数换算：按配方能量 500 kcal/100g 折为 0.0075 mg/100g
  折回条款单位：0.0075 mg/100g → 0.358509 ug/100kJ
影响分析: basis_changed ['target','维生素D','limit']；status_changed 标签 relabel -> fail
```

## 矛盾依据：停止并保留冲突

`data/bundle_conflict.json` 中两部无替代关系的现行标准对"铁"给出互斥区间
（6–12 与 14–20 mg/100g）：

```bash
$CLI import data/bundle_conflict.json
$CLI product data/product_lot_conflict.json
$CLI judge --lot LOT-C-SUPP-01 --date 2024-06-01   # 退出码 3
$CLI conflicts --lot LOT-C-SUPP-01                 # 冲突原文留档，不出具任何结论
```

## 漂移锁定

再次导入同编号同版本但内容被改动的规则包时，返回 `drift_locked`：原内容不被覆盖，
所有依赖该标准的判定停止，直到负责人处置：

```bash
$CLI drifts --status locked
$CLI resolve-drift --id DRIFT-xxxx --resolution "以首次入库文本为准" --by manager-wang
```

## 豁免（起草 / 批准分离）

```bash
$CLI exemption-draft data/exemption_lot_a.json
$CLI exemption-decide --id EXM-xxxx --by regulator-li --approve      # 拒绝：起草人=批准人
$CLI exemption-decide --id EXM-xxxx --by director-zhao --approve     # 通过
```

## 后补检测

```bash
$CLI add-test test_result.json   # 批次 open 时接收；decided 后拒收（kind=flow_closed）
```

缺检测时判定以 `stopped / missing_test` 返回（不落冲突档），补料后重新判定即可。

## 可中断的批量迁移

```bash
$CLI migrate --campaign CMP-2026Q4 --date 2026-10-01 \
     --lots LOT-A-2022-11,LOT-B-2023-05,LOT-C-SUPP-01 --limit 1
# 随时 Ctrl-C；用同一命令续跑：已 done 的批次跳过，conflict/blocked 的批次排在本轮
# 未处理批次之后重试，不重复生成决定
$CLI migration-status --campaign CMP-2026Q4
```

## 结果与退出码

| 结果 | 含义 |
| --- | --- |
| `compliant` | 合规 |
| `relabel` | 换标（限值合规，标签不符但仍在旧标签库存期内） |
| `halt` | 停止流转（限值不符，或标签宽限期已满） |
| `stopped` | 判定停止：矛盾依据 / 漂移锁定 / 方法不可比 / 缺检测，未出具结论（退出码 3） |

项目只使用 Python 标准库与本地 SQLite，无需其他运行服务。
