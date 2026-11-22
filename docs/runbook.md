# 运行手册：从原始索引到清单使用

本项目把多厂机器人实操录制的合成原件（`sources/`、`assets/`）加工为
可被离线作业直接读取的样本与谱系，并支撑训练/盲测划分、发布确认、
迟到凭证污染审计和新旧基准比较。只依赖 Python 3.11 标准库。

## 1. 核心概念与保证

| 概念 | 定义 |
| --- | --- |
| 作业 episode | 一次物理演练，如 `A`…`H`；片段、校准、标签都挂在作业上。 |
| 片段 segment | 某通道（cam/ctl）在设备刻度上的真实覆盖 `[lo,hi)`，左闭右开。 |
| 校准 calibration | cam/ctl 各两个锚点定义的线性时基；**锚点外没有对应关系，禁止外推**。 |
| 标签 label | 以 cam 帧为单位的动作区间。`reviewed` 才可交付；`pending` 是未审标签，与「无动作」是两种状态。`label_rule` v1/v2 不可互换。 |
| 修订 applies | 新记录 `applies` 指向被替换 id，空值为新增。被替换记录在替换到达后失效，但旧时点构建仍可复现。 |
| 凭证 links | 同一物理演练的 repeat/mirror/replay 关系；按传递闭包形成同源隔离组。只认有编号的凭证；共用场址/班次不构成联系。 |
| 样本身份 sample_id | 由作业、标签修订链根、边界、词表、校准、映射区间和所用片段算出的内容哈希（`smp-` 前缀）。任一输入变化都会换编号，换编号无法洗白来源。 |

### 可交付判定（全部满足）

1. 生效标签存在且 `state=reviewed`；
2. cam 通道完整覆盖标签区间；ctl 通道完整覆盖校准映射后的区间（分数精确比较）；
3. 整个标签区间落在生效校准的锚点之内；
4. cam、ctl 两个约定通道都在；跨文件片段只在边界邻接时拼接，断点保留为缺口。

不可交付时样本仍留在湖内并给出 `blockers`，状态取值：
`pending_label` / `no_label` / `no_calibration` / `outside_anchors` / `missing_channel`。

### 隔离与发布保证

- 隔离单位是同源组（含传递闭包），整组及派生物同进同出，**禁止拆组填表**。
- 历史清单（`past_manifests`）与实际回执（`exposures`）都是后续约束：
  - `train_exposed` 组不得进入新盲测；
  - `blind_exposed` 组的曝光动作不得转训练、不得重复盲测；
  - 迟到凭证使同组同时出现 train/blind 成员时，组状态为 `both_exposed`，整体隔离。
- 新发布只使用 `fresh` 组。覆盖下限与盲测体量上限按 `(site, tag)` 动作计数；
  无法同时达成时输出每个作用域的缺口和**卡住的来源组及原因**，不放宽约束。
- 预览带保证指纹；确认时复算，预览后若有新校准/标签/凭证/回执/片段/覆盖表到达，
  旧候选判 `candidate_stale`，必须重新预览。
- 历史发布不回写：污染只追加标注。重新导出或计算可比成绩时，污染区段不是有效盲测。
- 新旧基准只在「双方有效、同标签同校准（动作内容、边界、口径一致）」的共同样本上
  逐样本比较；成绩按评估方并列，不做整包平均分，题数相同也不比包分。

## 2. 目录与模块

```
corpus/
  intervals.py   区间合并、断点保留、线性校准（分数运算、禁外推）
  model.py       不可变登记台账 Store、as-of 快照 Snapshot、applies 修订链
  intake.py      原件加载、字段/引用/资产校验
  build.py       可交付性评估、样本身份、重建差异、样本包导出
  isolation.py   并查集同源组、曝光台账、冻结状态、迟到凭证污染审计
  planner.py     整组 train/blind 穷举规划、上下限约束、短缺与卡点报告
  workflow.py    预览指纹、时效守卫、发布登记
  benchmark.py   历史基准有效性、共同样本逐样本比较
  trace.py       从作业或已发成员追溯可用性、隔离依据、可比范围
  cli.py         命令行入口
tests/           unittest 套件（48 个用例，含既有源交付测试）
```

## 3. 本地重走流程

在仓库根目录执行；`-I` 隔离模式下用脚本路径启动（脚本会自行把仓库根加入 `sys.path`）：

```sh
# 0. 既有盘点与源交付校验
python -I source_inventory.py
python -I -m unittest discover -t . -s tests -v

# 1. 入湖：登记三批原件，校验字段、引用、CSV 资产
python -I corpus/cli.py --workdir out ingest

# 2. 按 as-of 时点构建样本湖（1/2/3 分别对应三批交付之后）
python -I corpus/cli.py --workdir out build --as-of 1
python -I corpus/cli.py --workdir out build --as-of 2
python -I corpus/cli.py --workdir out build --as-of 3

# 3. 重建差异：哪些旧样本失去对应、哪些只是边界/校准/词表变化
python -I corpus/cli.py --workdir out rebuild-diff --old 1 --new 2
python -I corpus/cli.py --workdir out rebuild-diff --old 2 --new 3

# 4. 同源隔离组与冻结状态
python -I corpus/cli.py --workdir out groups --as-of 3

# 5. 候选划分预览（不可行会退出码 2，并落盘 shortage 报告）
python -I corpus/cli.py --workdir out preview --as-of 2

# 6. 新证据到达后的时效守卫；旧候选确认会被拒绝（退出码 3）
python -I corpus/cli.py --workdir out stale
python -I corpus/cli.py --workdir out confirm --release r3

# 7. 迟到凭证污染审计、历史基准解释、新旧基准共同样本比较
python -I corpus/cli.py --workdir out audit --old 2 --new 3
python -I corpus/cli.py --workdir out benchmark
python -I corpus/cli.py --workdir out compare --left bench-r1 --right bench-r2

# 8. 追溯：作业视角与已发清单成员视角
python -I corpus/cli.py --workdir out trace --episode C --as-of 3 --prior 2
python -I corpus/cli.py --workdir out trace --member previous-d

# 9. 导出离线样本包（原始采样值 + 定位 + 标注 + 谱系）
python -I corpus/cli.py --workdir out export --as-of 2
```

全部 JSON 产物写入 `out/`（已在 `.gitignore`）。退出码约定：
0 成功；1 入湖校验有问题；2 候选不可行；3 发布被守卫拒绝；4 候选已过期；5 导出样本不可用。

## 4. 本数据上的预期结论（验收对照）

### 样本可用性随证据变化

| 作业 | as-of 1 | as-of 2 | as-of 3 |
| --- | --- | --- | --- |
| A | 可交付（cam 跨 `a1/a2` 邻接拼接） | 同左 | 同左 |
| B | 可交付 | 同左 | 同左 |
| C | 缺口：ctl 仅 `[100,114)`，映射需要 `[110,124)` | 补齐 `ctl-c2` 后可交付 | 新校准 `cc2` 使映射变为 `[114,128)`，覆盖只到 124 → **失去对应（lost）** |
| D | 可交付 | 同左 | 同左（但隔离状态变化） |
| E | `pending_label`（未审，不是无动作） | `le2` 替换 `le` 且 reviewed → 可交付 | 同左 |
| F | 可交付 | 同左 | 同左 |
| G | 可交付（v1） | 同左 | `lg2` 为 **v2 口径**，身份变化且与 v1 不可互换 |
| H | 可交付 | 同左 | 同左 |

### 隔离组（as-of 3）

- `{A,B,C,D}`：`link-ab` + 批次 3 的 `link-bd`、`link-bc` 传递成组；
  A 有训练成员、D 有盲测成员 → `both_exposed`，整体隔离。
- `{E}`、`{F}`、`{G}`：`blind_exposed`；`{H}`：唯一 `fresh`。

### 污染审计（凭证在双方交付后才到）

- 污染盲测成员：`previous-d`、`next-d`（bench-r1、bench-r2 各一）；
- 同组训练方见过的样本：`previous-a`（team1）；
- F、G、E 的盲测成员保持有效；历史清单原样保留，只追加污染标注。

### 基准比较 bench-r1 vs bench-r2

- 共同有效样本：标签 `lf`、`lg`（同标签同校准，逐样本并列两方成绩）；
- 排除：D（污染）、`next-e`（r2 独有，无共同对应）；
- 剩余覆盖随结果输出，不产生任何整包平均分。

### 分包可行性

- 三个 as-of 时点对当前覆盖约束都**不可行**：as-of 3 时 east/grip 的
  fresh 可用量为 0（A/B 组训练冻结、G 盲测冻结），west/mount 也无 fresh 来源
  （D 盲测冻结、C 被并入 both_exposed 组且自身不可交付）；
- 报告逐作用域给出 deficit 与 blocking_groups；H 单独满足 west/grip 下限。
- 规划器带合成场景测试证明：可行时它给整组方案、遵守上限且输出确定性。

## 5. 接收新交付的操作流程

1. 把新一批 `NN-*.json` 放进 `sources/`（文件名前缀为新到达序号），新 CSV 放进 `assets/`；
2. 重跑 `ingest`，确认 `validation_problems` 为空；
3. 对新 as-of 跑 `build` 与相邻时点的 `rebuild-diff`，核对 lost/new/边界变化；
4. 跑 `audit` 检查新凭证是否污染历史基准，跑 `groups` 看冻结状态；
5. 重新 `preview` → `stale` 守卫通过后才 `confirm`；
6. 需要给离线作业时 `export`，下游核对以 `bundles/index.json` 的 `lake_sha256` 为准。
