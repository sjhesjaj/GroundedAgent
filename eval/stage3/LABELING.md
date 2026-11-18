# Stage 3 · temporal / freshness 专项开发集：标注规则

数据集：`eval_temporal_freshness_dev.json`（29 条）。

- 这是一个**开发集**：Stage 3 修改 Planner 时会反复查看，所以它衡量的是开发进度，**不能用来衡量泛化能力**。
- 标签在任何 Planner 修改**之前**编写并单独提交。编写标签时没有运行当前 Planner，标签不参考 Planner 的输出。

## 要回答的问题

一个带时间词的请求，是否需要**实时读取一个会随时间变化的业务状态**？这就是 `requires_freshness` 的含义。

| `temporal_intent` | 含义 | `expected_requires_freshness` |
|---|---|---|
| `live_state` | 问的是一条业务记录此刻的值，比如库存、审批进度、个人余额或额度。这个值与制度文本无关，会随时间变化，只能从业务系统读取 | **True** |
| `current_knowledge` | 问的是当前有效的制度或规定写了什么。"目前 / 现行 / 最新 / 当前"等词修饰的是制度本身 | **False** |
| `mixed` | 同一个请求里既问制度，又问实时业务状态 | **True**（由实时业务状态那部分决定） |
| `incidental` | 句子里有时间词，但它的意思不是"当前是否有效"。例如：期限（"截至每年什么时候"）、紧迫（"我现在就要出差"）、名词的一部分（"实时通讯软件"）、"最近的工作日"（指距离最近），或者描述提问人自己的处境（"当前阶段是试用期"） | **False** |

## `requires_freshness` 语义契约（Stage 3 定稿）

以下契约对 Planner、全部标签和 Diagnostic Eval 都适用。

**定位：`requires_freshness` 是一个显式时效证据约束，不是"这是不是一个实时数据查询"的标记。**

- **它的作用：** 当 flag 为 True 时，Evidence Policy 会额外要求：至少有一条证据能证明答案是当前时点的值，也就是带 `observed_at` 的系统证据，或者带版本 / 观测时间的文档证据。如果找不到，就以 `freshness_unsupported` 拒答。
- **它不决定是否读系统。** 路由由 `needs_system` 决定，与 freshness 无关。
- **普通系统查询即使 flag 为 False，用的也是带 `observed_at` 的系统证据：**
  - `system_provider` 总是把记录自身的 `updated_at` 写进 `observed_at`，不会用查询时间代替。
  - Evidence Policy 对**每一条**系统证据都会检查 `observed_at`，这一步不看 flag。缺少 `observed_at` 会触发 `system_evidence_missing_observed_at`，这是阻断性原因，会直接拒答（见 `orchestration/evidence_policy.py`）。
  - 所以"SKU-A100 还有货吗"这类不带时间词的系统问题，flag 为 False，答案仍然来自带 `observed_at` 的实时记录。flag 为 True 时，只是**再显式要求一次**这种证据必须存在。
- **因此两种错误的代价不对称：**
  - **误报**（把制度问题标为 True）会给一个只有文档证据的请求加上无法满足的约束，这个问题必然被拒答。
  - **漏报**（实时请求被标为 False）只是少了一次显式约束。只要路由正确，系统证据本来就带 `observed_at`，回答不受影响。

**定义：** 当且仅当请求要求一个**实时业务状态的值**，并且用时间表达把这个值限定在**当前时点**时，`requires_freshness = True`。

- **实时业务状态**指业务系统里一条记录此刻的值，比如库存、订单状态、审批进度，或者本人的余额、额度、排班。它会随时间变化，只能从业务系统读取。制度、规定、流程的内容都**不算**，即使前面有"目前 / 现行 / 最新"这样的修饰。
- **时间表达**指"现在、目前、当前、实时、截至目前、今天、最近"这一类词。期限（"截至每年"）、紧迫（"我现在就要"）、名词的一部分（"实时通讯软件"）、提问人给出的前提（"我当前还在试用期"）都不算。
- **两个条件必须同时满足。** 只有时间表达、没有实时状态请求的，是 False。反过来，只有实时状态请求、没有时间表达的，按契约也是 False，这与冻结数据集的标注一致，那里不带时间词的系统问题都标 False。
  - 后一种情况在运行时没有区别：计划里有系统步骤时，系统证据一定带 `observed_at`，freshness 检查必然通过。

**运行时含义：** `requires_freshness` 在运行时只有 Evidence Policy 在用。它的意思是："这个请求的答案必须有能证明它是当前值的证据"，而目前只有带 `observed_at` 的系统证据能提供这种证明。所以一旦把制度问题错标为 freshness，这个问题就必然被拒答。

**实现的近似程度**（`orchestration/planner.py`，`_clause_requires_freshness`）：

- Planner 按**子句**判断：时间表达和实时状态请求要落在同一个子句里才算。实时状态请求指 `needs_system`，或者"第一人称 + 还剩 / 余额"这类个人余额。
- 两者被逗号分进不同子句时会漏判，例如"截止到现在，我的调休余额还有多少"。这是已知的漏报（见 `HOLDOUT_REVIEW.md` 和 HANDOFF §15），在运行时没有影响，Stage 3 不再继续修。

**与本数据集表格的关系：**

- 上表 `live_state` 一行写的是"True"。本数据集里所有 `live_state` 的 case 都带时间表达，所以与契约一致。
- holdout 是按上表编写的，其中 tfh_018（"sku-a100 还有货吗？够不够发 50 件？"）没有时间表达，却被标为 True。**按契约它应该是 False。** holdout 已经开封，结果按原标签如实记录，不会重新打分。

## 为什么"当前有效知识"不算 freshness

- **知识库本身就代表当前生效的版本。** 旧文档被新文档替代后，会同时从检索索引和 Wiki 中退役（见 `docs/v1/APP_DETAILS.md` 的 Wiki 生命周期）。所以"目前的制度规定了什么"只是一个制度内容问题；制度是不是最新的，由知识库的维护流程保证，不需要在每次请求时实时读取。
- **如果把它标成 freshness，这类问题就永远答不上来。** 文档证据不带版本号或观测时间（`document_adapter` 固定写 `version=None`、`observed_at=None`），Evidence Policy 一定会以 `freshness_unsupported` 拒答。这正是 validation 集里 h008 和 dev 集里 `answer_document_008` 失败的原因。
- **"最新 / 最近调整后"指的是版本，但问的仍然是规则内容，所以同样标 False。** 只有当请求要的是一个业务记录此刻的值时，才标 True。

## 路由标签

`expected_route` 是次要指标，用来确认修改 freshness 时没有把路由改坏。标注原则如下：

- 只问具体规则、数值或流程：`document_only`。
- 只要概览或介绍：`wiki_only`。
- 要业务记录的值：`system_only`。即使 demo 业务系统目前只开放库存查询，个人余额、审批进度之类在运行时会得到能力边界文案，但规划层面它仍然是 system 请求。
- 同时问制度和业务状态：按请求的组成部分组合路由。

## 覆盖的时间表达

目前、当前、现在、最新、今天、最近、现行、截至目前；另有当下、眼下、实时、此刻、截至。

前 8 个主要表达在 `current_knowledge` 和 `live_state` 下都至少出现一次，所以同一个词在两种意图下的判定都能被测到。其中当前、现在、最近还出现在 `incidental` 下。

当下、眼下、此刻、截至各只出现在一种意图下，它们只是补充覆盖。"截至"用于"截至每年……"（期限）；"截至目前"单独算一个表达。

## 已知的标签冲突（必须在修改前说明）

现有的冻结数据集里，有 2 条路由标签按"出现时间词就是 freshness"来标：

- dev：`route_document_only_004`，"现在这版考勤制度对迟到是怎么规定的？"，`expected_requires_freshness=True`
- validation_v1：`route_document_only_h005`，"目前生效的这版保密制度是怎么写的？"，`expected_requires_freshness=True`

按本规则，这两条应该标 False。与此同时，dev 的 answerability 数据集把 `answer_document_008`（"现在这版制度里，迟到多少分钟不扣款？"）标为应该回答，这和本规则一致。

冻结的数据集不会被原地修改。这两条的变更记录在单独的、带理由的修订文件 `eval/label_revisions/stage3_freshness_contract.revisions.json` 里。

- `eval/apply_label_revisions.py` 会把原标签分数和修订后分数并列报告。
- Stage 3 的结果：dev 和 validation_v1 的 freshness 信号在原标签下都是 79/80，修订后都是 80/80（`eval/stage3/route_eval_with_revisions.json`）。
- 这两条永远不会算作 Planner 的改进。
- `tests/test_label_revisions.py` 会校验这份修订文件：数据集的 LF 规范化哈希必须与记录一致，而且原数据集里的旧值必须与 overlay 记录的一致。

## holdout 状态：已开封，不能再作为 holdout

- `eval_temporal_freshness_holdout.json`（封存于 `7cc5abe`，sha256 `b24d326e…`）已经在 `c786cf4` **打开过一次**，结果见 `holdout_result.json` 和 `HOLDOUT_REVIEW.md`。
- 开封之后，它的内容已经被实现者看过，并且做了逐条分析。**它不能再作为 holdout，也不能用来衡量任何后续修改的泛化能力。** 以后只能当作一份已见过的回归集使用。
- `eval/stage3_freshness_holdout.py` 发现结果文件已存在时会拒绝再次运行，所以不会发生第二次开封。
- 后续对 freshness 的任何修改，如果需要衡量泛化能力，都必须先准备一份**新的、隔离的** holdout。Stage 3 不做这件事。
