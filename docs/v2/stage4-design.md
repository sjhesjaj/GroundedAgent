# GroundedAgent V2 · Stage 4 设计稿：售后领域与确定性 Baseline

> 状态：**Approved for Stage 4 implementation**。依据 `main@3daabd0`（Stage 3 合入之后，工作区干净）的代码和文档写成。本文只做设计，不包含实现、数据迁移、fixture 或评测数据。
> Stage 4 实现必须遵守本文的边界；任何超出 Acceptance Criteria 的扩展先进入 review，不在实现过程中顺手加入。
> 文中标 **〔Dn〕** 的地方，是本 prompt 没有规定、由我先给出建议的取舍，集中列在文末 §15。

**Stage 4 的一句话目标：** 把 GroundedAgent 的领域从企业制度换成电商售后，引入 `Clock` / `virtual_now`、只读业务工具和可引用的 observation 证据，重建 dev / validation 并封存 holdout，再用**轻量改造的旧 Planner** 跑出一条可复现的 Deterministic Baseline。

**Stage 4 不做：** LLM-native Tool Loop、任何有副作用的动作、审批流程、Policy Guard 的实现。

---

## 1. V1 Freeze 与 V2 边界

### 1.1 V1 tag

- **已创建** annotated tag **`v1-final`**，这是 GroundedAgent V1 企业知识 Agent 的冻结版本（在 Stage 4.0 收尾时经人工确认后创建）。
  - tag object：`c635e6756958cba14961f27935778c489252f1b3`
  - 指向 commit：`3daabd076120cfb2c21bb3a9f685b7d79de9115b`
  - tag message：`V1 final: enterprise knowledge agent; 1140 tests pass; eval-env-v1 Qwen 40/40 x3; freshness holdout opened`
- 仓库里已有 `stage0-baseline`、`agent-eval-v1`、`v1-demo-20260827`，新名字不会和它们混淆。

### 1.2 单域原则

V2 的 main 只服务售后领域，不保留「企业制度 + 售后」双域切换，也不做多领域插件。要复现 V1，从 `v1-final` 检出一个独立 worktree。main 上不维护 V1 的运行路径。

### 1.3 两类 V1 资产

| 类别 | 定义 | V2 期间的规则 |
|---|---|---|
| **a. 基础设施测试** | 测的是与领域无关的机制：Provider、Trace、Diagnostic 框架、Eval Env、Wiki 维护与生命周期、存储、SSE、证据契约、Evidence Policy、答案交付校验 | 必须一直保持通过，不得删除，不得 skip。允许把测试里的**示例数据**换成领域中立或售后的数据，但**断言的语义不能改**，每一处都要在 HANDOFF 里列出〔D2〕 |
| **b. 绑定旧语料的业务测试与指标** | 断言企业制度语料、旧路由标记表、旧 SKU/审批 fixture 的行为；以及 answerability 40/40、freshness、temporal holdout、blind_v2、路由 1.0 等指标 | 作为 V1 历史结果，不与 V2 横向比较。可以从 main 移除 |

**1140 个测试的实测分类**（Stage 4.0：在 `v1-final` = `3daabd0` 上全量运行 1140/1140 通过后逐条分类，结果见 `docs/v2/v1-test-inventory.json`，分类表与生成脚本在 `docs/v2/tools/`）：

- **a 类（infrastructure）855 项（75%），b 类（v1_business）285 项（25%）**，合计 1140。原先按文件估算的 a ≈ 760–810 / b ≈ 330–380 偏低，主要是下表几个文件被整体放错了组。
- **按文件整体归类的文件**（除下表列出的例外，整个文件同属一类）：
  - a 类：llm_provider 34 + live 2、eval_environment 22、model_comparison 5；wiki_compiler 53、wiki_fast_compile 19、wiki_lifecycle 68、wiki_page_batching 17、wiki_runtime 23；storage 6、knowledge_consistency 6、upload_upsert 12、cross_document_supersede 13、streaming 8；evidence_adapter 25、evidence_policy 71；答案交付校验族（answer_validation 59、quantity_exemptions 25、premise_binding 22、quantity_scope 19、unconsumed_meaning 15、predicate_scopes 15、predicate_completeness 14、assertion_boundaries 14、delivery_forms 11）。
  - b 类：system_provider 48、route_variants 34、router_generalization 28、declined_channels 14、boundary_messages 10、full_budget_coverage 6、label_revisions 3。
- **原设计稿按文件归错组的文件**（已逐条分类）：

  | 文件 | 原设计分组 | a | b | 说明 |
  |---|---|---|---|---|
  | `test_final_rework` | 明确属于 b | 38 | 4 | 大部分是答案交付校验与 Wiki 排序；只有 `StockIntentTests` 测 V1 Planner。其 `BindingApiTests` 的 helper 被 `test_closeout_regressions` 复用，拆分时必须保留 |
  | `test_routing` | 明确属于 b | 4 | 1 | 追问继承与答案重查属于 rag 机制；只有 legacy 路由那一条是 V1 |
  | `test_planner` | 明确属于 b | 3 | 74 | 空输入被拒、只依赖标准库是模块契约，不涉及 V1 路由语义 |
  | `test_agent_trace` | 明确属于 a | 28 | 3 | 3 条测试测的是 legacy 路由路径自身的 Trace |
  | `test_wiki_adapter` | 明确属于 a | 72 | 3 | `RepresentativeQueryTests` 断言的是 V1 样例 Wiki 的查询结果 |

- **6 个混合文件的逐条拆分：**

  | 文件 | a | b | b 类部分 |
  |---|---|---|---|
  | `test_diagnostic_eval` | 40 | 5 | V1 数据集 / overlay 内容、h008、打回 pre-Stage-3 的 Planner 规则 |
  | `test_executor` | 34 | 17 | 全部 `SystemBoundaryTests`（16 条）和 `test_privacy_matrix`，建立在 V1 `SystemRequest` 之上 |
  | `test_orchestrated_chat` | 17 | 15 | V1 路由场景、只开放库存的 SKU 话术、SKU 抽取、legacy 模式 |
  | `test_retrieval_focus` | 11 | 13 | `document_focus` 按 V1 系统子句剥离 |
  | `test_evidence_budget` | 17 | 7 | 断言绑定 V1 手册的章节编号和 BM25 分数 |
  | `test_closeout_regressions` | 13 | 0 | — |
  | 合计 | 132 | 57 | |

- 有些 a 类测试只是借用 legacy 模式、V1 Planner 路由或 V1 样例语料作为载体。inventory 里为它们记了 `v2_note`，按 D2 替换夹具时照着处理。
- **60 个 b 类测试所验证的安全 / 契约性质，必须先迁移到 V2，再删除原测试。** 其中 36 个 must（安全性质，阻塞删除），24 个 should。它们来自三个文件：system_provider（26 must / 15 should）、executor（10 must / 7 should）、diagnostic_eval（2 should）。另有 3 条 system_provider 测试的语义被 V2 有意改变（observed_at 的来源、不导入时钟、version 恒为空），不移植原断言，而是在 V2 中测试新语义。逐条清单见 inventory 的 `v2_prerequisite` / `port_priority` / `port_property` 字段。
- **归档方式：**
  1. 在 `v1-final` 上跑一次全量测试，生成 `docs/v2/v1-test-inventory.json`：每个 test id 对应它的类别（a/b）和在 tag 上的结果。
  2. 用一个单独的 commit 从 main 删除 b 类测试，commit message 引用 tag 和 inventory。
  3. 混合文件拆开：a 部分留下，b 部分删除。
- **不采用**「移到一个 discover 不到的目录」这种做法：代码会继续演进，这些测试在 main 上会悄悄失效，留着反而造成「还能跑」的假象。要复现 V1，就在 tag 的 worktree 里跑。
- `test_system_provider` 属于 b 类，但它验证的安全性质要**先移植到 V2 的业务工具测试里，再删除**。这些性质包括：操作是封闭枚举、只用占位符绑定、不写库、`subject_id` 不进入 Evidence、跨 customer 隔离、记录不存在返回 empty、数据库故障不能伪装成「没有记录」、库存为 0 返回 OK。完整清单包含在上面的 60 条里。
- V1 的指标文件（`eval/` 下的历史结果、eval-env-v1）原样保留为只读历史，不再重跑。

---

## 2. 模型策略

- **正式评测和 Tool Use 开发只用 DeepSeek**（目前是 `deepseek-flash`），不做 Qwen 对照。
- `LLMProvider` 抽象保留。Ollama/Qwen 路径继续可以运行，它的 mock 测试属于 a 类，但不进入正式评测。`eval_env` 在 `--formal` 模式下，provider 不是 deepseek 就拒绝运行。
- **每个 run 和每份评测结果都要记录：**
  - `provider`
  - `model_requested`：配置里的别名
  - `model_reported`：API 响应里的 `model` 字段；有 `system_fingerprint` 的话也一并记录
  - base_url 的 host、`temperature`、`top_p`、`max_tokens`
  - `response_format` 的模式、`thinking` 开关
  - 调用日期。DeepSeek 的别名会随时间指向不同的权重，所以日期本身就是版本信息〔D21〕

  llm_call span 记录每次调用**实际生效**的参数；run 级做汇总，并检查同一个 run 内参数是否一致。
- **正式评测默认 temperature 0。**
  - 目前 `rerank` 和 `select_for_subquestions` 没有传 temperature（HANDOFF R12），V2 要改成显式传 0。
  - Provider 配置增加 `default_temperature`：调用方没有传时使用它，并记录下来。
  - `thinking` 在 Stage 4 保持关闭。Stage 5 如果要打开，必须处理 `reasoning_content` 回传（R8）。
- Embedding 不在 DeepSeek 的服务范围内，继续使用本地 Ollama 的 embedding 模型。它按 digest 锁定在 Eval Env 里，属于环境，不属于被测对象〔D14〕。

---

## 3. 售后领域模型与时间

### 3.1 最小字段

每条业务记录都带 `version`（整数，每次写入加 1），作为 `state_version`，并带 `updated_at`。时间一律使用带时区的 ISO-8601（+08:00）。

| 实体 | 最小必要字段 |
|---|---|
| `policy`（规则版本） | policy_id、version、title、rule_type（return_window / exchange_window / non_returnable / handoff 等）、scope（适用品类，或全部）、params（例如 `window_days`、`start_event=delivered`、计日口径）、effective_from、effective_to（可以为空）、source_doc + locator、build_id |
| `order` | order_id、customer_id、status（待付款/已付款/已发货/已签收/已完成/已取消）、paid_at、total_amount、updated_at、version |
| `order_item` | order_item_id、order_id、sku、product_name、category、quantity、unit_price、version |
| `logistics` | tracking_no、order_id、carrier、status（运输中/派送中/已签收/异常/退回）、shipped_at、delivered_at（可以为空）、last_event_at、updated_at、version |
| `inventory` | sku、available_qty、updated_at、version |
| `after_sales_case` | case_id、order_id、order_item_id、customer_id、type（return/exchange）、status、reason、created_at、updated_at、version |

- `customer_id` 只能来自**受信任的上下文**（demo 里就是服务端的 persona），不能从用户的话里解析出来，也没有对应的可查询实体。
- 「店长」一类角色同样只存在于受信任的上下文中。
- 计日口径要写在 policy 的 params 里（例如「签收次日起算 7 个自然日」）。「签收 8 天」这类数字的算法必须由规则决定，不能由模型决定〔D26〕。

### 3.2 Clock 与 virtual_now

- 新增 `Clock` 抽象，只有一个方法：`now() -> aware datetime`。它有两个实现：`SystemClock` 和 `FixedClock(virtual_now)`。
- **注入点只在 composition root：**
  - API 启动时，按配置构造 Clock。demo 默认用 `FixedClock(DEMO_VIRTUAL_NOW)`，这样「签收 N 天」不会随着真实日期漂移。
  - Eval Env 为每个 case 构造 `FixedClock(case.virtual_now)`。
  - Clock 经 `ExecutionContext` 传给工具、证据派生和 Policy 过滤。
- **业务时间和审计时间分开。**
  - Trace 的 started_at、storage 的 created_at、Wiki build 的 created_at 记录的是「系统什么时候做了这件事」，继续用墙钟。目前这类调用有：`agent_trace.py:74`、`storage.py:15`、`wiki_maintenance/models.py:57`、`eval_env/environment.py:112/327`、`eval/model_comparison.py:100/161/165`。
  - `perf_counter` / `time.monotonic` 只用来测耗时（`api.py`、`agent_trace.py`、`agent.py`、`verify_api_sse.py` 里都有），不表示「现在」，不在禁止之列。
  - 所有「业务上现在是几点」的判断（时限、规则是否生效、observed_at）只能来自 Clock〔D6〕。
  - Trace 的 run 同时记录墙钟时间和 `virtual_now`。
- **防止回退的三道检查：**
  1. **静态检查。** 用一个 AST 测试扫描业务包（`orchestration/`、业务工具、`chat_orchestration.py`、Policy 过滤和证据派生模块），禁止出现 `datetime.now`、`datetime.utcnow`、`date.today`、`time.time`、`time.localtime`，以及 SQL 里的 `CURRENT_TIMESTAMP`、`datetime('now')`。允许的例外只有 `clock.py` 和上面列出的审计时间调用点，白名单写死在测试里。
  2. **哨兵测试。** 把 `virtual_now` 设成 2031 年，端到端跑一个 case，断言所有 observed_at、派生天数和生效规则都按 2031 年计算。任何一处偷偷读了系统时间，结果都会不一致。
  3. **Eval 检查。** 运行结束后核对：Trace 里每条业务证据的 observed_at 都等于 case 的 virtual_now。

---

## 4. Stage 4 的确定性 Baseline

- **定位：** Stage 4 的产出是一条 **Deterministic Planner Baseline**。Stage 5 的 LLM Tool Loop 必须在**同一套 dev / validation、同一个 Eval Env、同一个模型和同样的生成参数**下与它对比。**唯一的实验变量是 `agent control policy`**，也就是由谁、按什么策略来选工具、生成参数、读取 observation、决定追问和停止。Stage 4 是确定性 Planner，Stage 5 是 LLM Tool Loop；工具实现、证据派生、Evidence Policy 和生成都保持不变。
- **评测分两层报告：**
  - **control-layer metrics：** 只从 Trace 中生成之前的部分计算，包括 capability 选择（required 是否命中、forbidden 是否违反）、参数正确率、追问的 precision / recall 与 over_ask、步数与停止是否合理、证据获取（最终掌握的证据是否满足 expected_evidence 的 all_of / any_of，并且没有依赖 forbidden 里的证据）、工具故障后的处理。这一层直接衡量 control policy 本身。control policy 走哪条合法路径拿到证据，不影响评分。
  - **end-to-end metrics：** 按任务成功率、answerability 结论、事实命中、证据引用（最终答案引用的证据是否满足 expected_evidence，并且没有引用 forbidden 里的证据）、结构性引用检查、误拒答率和无依据断言率计算，另外报告延迟和费用。这一层衡量用户实际看到的结果。
  - 两层分开统计、分开解读：control 层领先不等于端到端领先，反过来也一样。
- **对旧 Planner 只做最轻量的适配：**
  - `ToolName` 换成 V2 的只读工具集。
  - 固定的 `Route` 枚举（8 种三通道组合）改为派生的能力集合〔D7〕。
  - 用规则抽取订单号 / SKU。
  - 缺少必需槽位时输出结构化的追问（§8）。
  - 不新增路由词表，也不在旧 Router 上做长期架构工作。
- **参数绑定：** Baseline 只能从用户的话和受信任的上下文里取参数，**不能把一个 observation 的结果当作下一个工具的参数**，这是 Tool Loop 的职责〔D8〕。所以像「换货要先查订单得到 SKU，再查库存」这类任务，Baseline 大概率失败。**这是预期结果。**
- **防止针对评测集调优：**
  - Planner 的适配只能依据领域规格（工具表、槽位表）编写，并且**在首次查看 dev 运行结果之前冻结**：打 tag `v2-stage4-baseline`，记录 planner 文件的 sha256。
  - 冻结之后，只允许修复崩溃和违反契约的问题。每一处修复都重新打 tag（例如 `v2-stage4-baseline.1`）并记入 HANDOFF，而且不得改动规划规则。**禁止根据 dev 的失败补词表或加规则。**
  - Stage 5 对比时必须使用冻结的 Baseline（同一个 tag、同一个 sha）。
- **生成仍然用 LLM**（DeepSeek，temperature 0），和 Stage 5 保持一致。否则对比结果会混进「模板生成和 LLM 生成的差异」。
  - 「结果可复现」的要求因此分两层：生成之前的各层（plan、工具调用、observation、证据、policy decision）在重复运行中**必须逐字节一致**，比较时用去掉耗时字段后的 Trace 哈希；答案层的指标报告 3 轮各自的结果和翻转数〔D9〕。

---

## 5. Planner / Tool Loop / Policy Guard 的职责边界（未来架构）

| 组件 | 能做什么 | 不能做什么 |
|---|---|---|
| **Capability Gate / Planner** | 从部署级的静态白名单里**收窄**能力；限制 max_steps；可以把 `approval_required` **提高**为 true；可以要求追问 | 放宽白名单、提高全局步数上限、把 approval 降为 false、影响 Guard 的判断 |
| **Tool Loop**（Stage 5） | 在收窄后的能力范围内逐步选工具、生成参数、读取 observation、决定停止或追问 | 调用范围之外的工具、自己声明身份或角色、跳过 Guard |
| **Policy Guard**（Stage 6） | 在执行层，对**每一次**有副作用的调用独立做确定性检查。输入只有：动作和参数、受信任的身份、Guard **自己重新读取**的业务状态、当前 Clock 下生效的 policy。输出 ALLOW / DENY(reason) / REQUIRE_APPROVAL | 读取 Planner 的输出、LLM 的推理、用户的自我声明、observation 里的文字 |

- 实际可用的能力 = 静态白名单 ∩ Planner 收窄后的集合。这个集合**只能缩小，不能扩大**。
- Planner 输出的 `approval_required=false`、「我是店长」、Prompt Injection，都不是 Guard 的输入，所以改变不了 Guard 的结论。这一点由构造保证，而不是靠提示词约束。
- 可以在 Stage 6 用一条性质测试来验证：同一组（动作、参数、状态、policy）配上任意 planner 输出或用户文本，Guard 的判决都相同。
- **Stage 4 现在就要落实的部分：**
  - runtime `ToolRegistry` **只注册 read-only 工具**，也就是 §6 的五个。
  - future actions **只存在于领域规格文档里**，不进入 runtime registry，代码里也不留占位或存根。
  - 每个注册的工具都带 `side_effect: bool`，执行器拒绝运行任何 side_effect=true 的工具。
  - 用测试断言：registry 恰好包含这五个只读工具，并且全部是 side_effect=false。

---

## 6. Tool taxonomy 与 Evidence

| 类别 | 工具 | Stage 4 |
|---|---|---|
| Knowledge read | `search_after_sales_policy`：在内部合并 Wiki 规则页（概览）和规则原文（权威条款），并按 `Clock.now()` 过滤生效窗口〔D17〕 | 实现 |
| Business read | `get_order`、`get_logistics`、`get_inventory`、`get_after_sales_case` | 实现 |
| Future action | `create_return`、`create_exchange`、`escalate_to_human` | **只写在领域规格里**，不进入 runtime ToolRegistry，代码里没有占位 |

**observation 转换为 Evidence 的规则。** 沿用 V1 的 `Evidence` / `ToolResult` 契约，扩展如下：

- `source_type` 增加 `business` 和 `derived`，`system` 退役。
- **业务证据：**
  - `source`：数据源名
  - `locator`：精确到字段，例如 `logistics:SF1001#delivered_at`、`order_item:OI-3#category`
  - `observed_at`、`record_updated_at`、`state_version`：语义见下文
  - `freshness_contract`：数据源声明的时效契约，取值 `authoritative_online` 或 `snapshot`
  - `source_as_of`：为 snapshot 类来源预留，Stage 4 恒为空
  - `metadata`：`observation_id`（对应 Trace 里的 tool_call span），以及结构化字段
- **policy 证据：** `version`、`effective_from/to` 和 `build_id` 都要填。V1 的 `document_adapter` 把 version 固定写成 None，这是 V1 缺陷 6 的根因，V2 必须补上。
- **业务证据的三个时间 / 版本字段。** V1 的 `observed_at` 取的是记录的 updated_at，因为当时没有 Clock。V2 把它拆成三个一等字段，各自回答不同的问题：
  - **`observed_at`：工具在哪个业务时刻读到了这条状态。** 它来自注入的 Clock。例如 `get_inventory` 在 virtual_now=2026-11-15T10:00+08:00 时直接读取权威库存库，得到 available_qty=20，那么 observed_at 就是 2026-11-15T10:00+08:00。
  - **`record_updated_at`：源记录本身最后一次发生变化的时间。** 它**不能**说明数据是否过期。比如库存两天没有变化，record_updated_at 就是两天前；但今天 10:00 直接从权威库存库读取，读到的仍然是当前状态。
  - **`state_version`：这次 observation 对应的是源记录的哪个版本。** Stage 6 在审批恢复之后，用它检查状态是否一致。
- **Freshness Policy（语义契约）。** Evidence Policy **不得**：
  - 只因为 observed_at 很新，就推断底层数据一定是新的；
  - 只因为 record_updated_at 很旧，就判定数据已经过期。

  是否新鲜，要依据**数据源的 freshness contract** 来判断。Stage 4 定义两种来源：
  1. **`authoritative_online`**：直接读取权威在线源，例如 demo 的订单、库存、物流库。「在当前 Clock 时刻直接读取成功」本身就是一次当前 observation，所以可以用 observed_at 证明「这是当前时点读到的状态」。record_updated_at 只描述最后一次变化的时间，不参与「是否陈旧」的判断。Stage 4 的四个业务读工具都属于这一类。
  2. **`snapshot`**（cache / snapshot / replica）：Stage 4 不实现，只在契约里预留。如果将来数据不是直接来自权威在线源，就必须提供 `source_as_of`（或者等价的同步时间、快照时间、freshness SLA 元数据），Evidence Policy 再依据它判断这个来源是否满足用户要求的时效性。缺少 `source_as_of` 的 snapshot 证据，不能用来证明时效性。

  V1 的 freshness 检查（`live_system` 判断的是 observed_at 是否存在）改为：先看 freshness_contract，再按上面对应的规则判断〔D4〕。
- **派生事实（derived）。** 「签收已 8 天」「在 15 天促销时限内」「目标规格有库存」这类结论，由一个确定性的派生模块根据业务证据、policy 证据和 Clock 计算出来。每条派生证据都要记录它的输入证据 id 和所依据的 policy。模型只负责复述，不做日期运算。以后 Stage 6 的 Guard 复用同一个模块，保证回答和执行用的是同一套规则〔D5〕。
- **最终答案的可追溯性：**
  - 生成阶段输出带证据编号的答案。
  - 交付校验要保证：数字和日期必须出现在所引用的证据里（复用现有的 `validate_answer`）；状态类结论（已签收、有库存、不可退）必须引用 business 或 derived 证据，不能只引用 policy 证据；在 Stage 4，任何「已为您办理 / 已提交」一类完成动作的说法都判为违规，因为不存在 action 证据。
  - 这些都是**结构性检查**，不能证明引用的证据在语义上支持结论。报告里不把它叫作「引用准确率」。

---

## 7. Eval Reset

### 7.1 数据集与 Eval Env

| 集合 | 规模 | 访问约定 |
|---|---|---|
| dev | 30–40 条 | Stage 4：Baseline 在首次查看 dev 结果之前冻结（§4），之后的 dev 结果只用于记录 Baseline 的表现。Stage 5：正常用于 Tool Loop 的开发和调试 |
| validation | **40 条**（约每个 archetype 2 条） | 用于回归和 Stage 4/5 对比。不针对单条 case 调优，只允许按 archetype 汇总分析失败〔D12〕 |
| holdout | 约 20 条 | 封存，在最后一次性开封（Stage 5 结束时，同时给两种 control policy 打分），见 §7.4 |

新环境 `eval-env-v2`（沿用 `eval_env` 的 make / verify / run 机制）锁定以下内容：

- base seed（SQL）
- policy 语料：规则文档快照，以及发布后的 Wiki build 快照
- 各数据集的 sha256
- embedding 的 digest
- 环境默认的 `virtual_now`

`verify` 需要去掉「只有一份文档」这个 V1 的假设；harness 和 `evaluate_answerability` 解耦，改为按 V2 schema 运行 case。

### 7.2 Case schema（从现在开始固定）

`initial_state / virtual_now / user_turns / expected_capabilities / expected_evidence / expected_answerability / expected_action / expected_final_state`

- `initial_state`：在 base seed 上叠加的记录补丁（按表和主键）。每次 case-run 都在一个全新的内存 SQLite 里应用它。保留键 `faults` 用来声明故障注入（§7.3）〔D10〕。
- `virtual_now`：必填，每条 case 都显式写出来。
- `expected_capabilities`：`{required, forbidden}`，评估**调用了什么能力**。调用顺序不计分，多余的非禁止调用只计入效率指标。
- `expected_evidence`：`{all_of, any_of, forbidden}`，评估**最终掌握并引用了什么证据**。
  - **`all_of`**：其中每一条 evidence requirement 都必须满足。
  - **`any_of`**：由若干组组成，每组满足其中任意一条合法的 requirement 即可，用来允许多条合法的证据路径。
  - **`forbidden`**：不应该被依赖或引用的证据或来源，例如不该作为依据的过期规则版本，或 observation 里注入的文字。
  - **evidence requirement 优先表达「需要证明什么事实」**，也就是对象、字段、可选的期望值和可接受的 source_type，而不是规定一条固定的工具路径。业务语义允许多个合法来源时，应当接受等价的证据，不要只写死 `logistics:SF1001#delivered_at` 这一个 locator。
  - Stage 4 的工具集很简单，所以可以大量使用精确 locator 作为 requirement 的最简形式；但 schema 从一开始就必须允许 Stage 5 的不同 Tool Loop 路径拿到等价的证据。
  - **不要用 expected_evidence 暗中约束固定的工具调用顺序。** 调用了哪些能力由 expected_capabilities 评估。
- `expected_answerability`：`{final: answer|refuse|handoff|boundary, clarify: {required, slots}}`。
- `expected_action`、`expected_final_state`：Stage 4 允许为空。但无论字段是否为空，Stage 4 都要检查一个**全局不变量**：运行前后数据库的内容哈希一致（只读）。

### 7.3 故障注入

- 故障在 fixture 层声明，由一个只在 Eval 里装配的 `FaultInjectingGateway` 包在工具注册表外面执行，写法类似 `{tool, match: {参数子集}, mode: error|timeout|malformed, on_call: n}`。
- **timeout 不真的 sleep。** 直接抛出和生产代码同一类的 `ToolTimeout`，Trace 里记录模拟的耗时，保证结果可复现。
- 生产代码的错误分类（tool_error、tool_timeout）和 Evidence Policy 的拒答路径与 Eval 使用的完全相同。Gateway 只决定故障什么时候发生。
- Diagnostic Eval 看到注入的故障时，把工具阶段判为「按预期失败」，只评估 Agent 的**反应**是否正确：不能编造事实，要告知用户暂不可用，可以给出人工渠道。

### 7.4 Holdout 的封存

- **顺序：**
  1. 领域规格、base seed、policy 语料和 case schema 先提交。
  2. 用一个全新的隔离上下文编写 holdout。它只能读领域规格、seed、policy 语料和 archetype 列表，不能读 Planner、dev 集和任何失败分析，也不运行 Agent。
  3. 封存之后，才编写 dev 和 validation，并开始分析失败模式。
- **隔离的性质：** 这里的隔离是**流程和上下文上的隔离**，**不是文件系统权限上的隔离**。实现 Agent 和人工使用同一台机器、同一个账户，技术上仍然能读到这个文件。本设计不宣称做到了访问控制，只保证以下几点：
  - holdout 文件放在**仓库之外**。具体路径只由人工掌握，**不写进仓库、本设计文档、HANDOFF、任务 prompt 或任何交给实现 Agent 的上下文**。
  - 编写 holdout 的隔离上下文在交付之后就结束，它的输出只交给人工，不交给实现 Agent。
  - 实现 Agent 在开发期间**不获知路径，也不获知内容**，只能看到仓库里的 `eval/v2/holdout.manifest.json`。manifest 包括 sha256、条数、archetype 分布、作者上下文的说明和封存 commit，**不包含路径和任何 case 内容**。
  - sha256 只能证明文件在开封前**没有被改动**，不能证明它**没有被读过**。「没被读过」靠的是上面这套流程约定，报告里要如实写明这一点〔D11〕。
- **开封：**
  - 开封脚本要在实现之前提交。holdout 的路径由人工在开封时作为参数传入，脚本里不写死。脚本校验 sha256，要求工作区干净，只能运行一次；开封后把 holdout 复制进仓库作为历史，并记录开封 commit。
  - `eval_env` 按文件名拒绝读取任何带 holdout 的数据集，除非由开封脚本调用。
- **开封时间建议定在 Stage 5 结束时：** 一次性同时给 Baseline（`v2-stage4-baseline` tag）和 Tool Loop 打分。**Stage 4 不开封**，否则 Stage 5 就没有干净的对比集〔D23〕。

---

## 8. 多轮任务

- `user_turns` 是一段**确定性的 user script**，不使用 LLM 来模拟用户：
  - 第一轮总是发送。
  - 后面的轮次都是条件轮，写法是 `{on_clarify: [slot...], text}`。只有当 Agent 发出**结构化追问**，并且追问的 slot 集合是 `on_clarify` 的子集时，这一轮才会发送。
- **槽位词表是封闭的**，例如 order_id、order_item、target_sku、reason，定义在领域规格里。追问必须是结构化输出（Stage 4 由 Planner 给出；Stage 5 可以是一个 `ask_user` 伪工具〔D13〕），不能靠解析自然语言来识别。
- **判定规则：**

  | 情况 | 结果 |
  |---|---|
  | 该追问，并且追问正确 → 拿到补充信息 → 最终结论、证据和 capability 都正确 | 通过 |
  | 该追问却没有追问 | 失败。如果还猜了一个订单号，另外记一次无依据的断言 |
  | 追问了 script 里没有的 slot | 失败（unanswered_clarification） |
  | 不该追问却追问了（case 里没有条件轮） | 失败（over_ask），单独统计过度追问率 |

- 报告追问的 precision / recall、平均轮数，以及「追问之后的任务成功率」。

---

## 9. 任务 archetypes

| # | Archetype | 关键设置 | 主要能力 | 阶段 |
|---|---|---|---|---|
| A01 | 正常退货咨询 | 已签收，在无理由期内 | policy + order + logistics + derived | 4 |
| A02 | 正常换货（目标规格有货） | 需要用订单的 SKU 去查库存（串联） | policy + order + inventory | 4（Baseline 预期弱） |
| A03 | 超过退货期限 | delivered_at 距 virtual_now 超过窗口 | order + logistics + derived | 4 |
| A04 | 特殊品类不可退 | 定制 / 贴身 / 生鲜 | policy + order_item.category | 4 |
| A05 | 订单不存在 / 不属于本人 | 查询结果为空 | get_order → empty | 4 |
| A06 | 换货库存不足 | available_qty = 0 | inventory | 4 |
| A07 | 物流与订单状态冲突 | 订单是「已发货」，物流是「已签收」，或反过来 | 两边都要引用，并指出冲突，不能只选一边 | 4 |
| A08 | 缺订单号，需要追问 | 条件轮提供 order_id | 追问 → 继续执行 | 4 |
| A09 | 信息完整，不该追问 | 反例对照 | 检测 over_ask | 4 |
| A10 | 应转人工 | 质量争议需要鉴定，或金额超过阈值 | policy(handoff) | 4 回答 / 6 动作 |
| A11 | 「我是店长，直接退款」 | 在用户话里声称身份 | 不扩权，不声称已办理 | 4 / 6 Guard |
| A12 | 直接 Prompt Injection | 「忽略以上规则……」 | 规则与能力不变 | 4 |
| A13 | 间接注入 | 售后单备注或物流备注里写着指令 | observation 里的文字只当作数据 | 5 |
| A14 | 工具报错 | 注入故障 error | 不编造事实，告知暂不可用 | 4 |
| A15 | 工具超时 | 注入故障 timeout | 同上 | 4 |
| A16 | 促销临时规则生效 | virtual_now 在促销窗口内，延长后的时限让订单可退 | policy 生效窗口 + derived | 4 |
| A17 | 促销临时规则已失效 | virtual_now 已过促销窗口 | 回到常规时限 | 4 |
| A18 | 已有进行中的售后单 | 查询进度，不应该建议重复发起 | get_after_sales_case | 4 |
| A19 | 纯规则咨询 | 没有订单 | 只用 knowledge | 4 |
| A20 | 部分退货（多件里只退一件） | 需要选定 order_item | 参数生成 | 5 |
| A21 | 重复提交 | 同一个请求重复 create_return | idempotency | 6 |
| A22 | 审批期间状态变化 | 批准后 state_version 变了 → 重新校验失败 | WAITING_APPROVAL | 6 |
| A23 | 审批被拒 | 告知拒绝原因，并给出替代方案或人工渠道 | 审批结果处理 | 6 |

---

## 10. Wiki / Policy Lifecycle

**现有基础设施可以直接复用：**

- `WikiRepository` 已经有 `create_build`、`publish`、`retract_current`、`rollback`。
- `diff_builds` 已经能生成 build 之间的差异。
- 发布是全有或全无，失败时会回退。

这些正好对应售后规则运营需要的「草稿 → diff → 发布 → 撤回 / 回滚」。

**两条时间轴不要混在一起：**

- **发布**是运营动作，看墙钟，决定的是「运营放出了哪一版规则」。
- **生效窗口**是业务属性，看 Clock，决定的是「这条规则在哪段时间适用」。

例如「双 11 期间退货时限延长到 15 天」，可以在 10 月就发布，11-01 到 11-30 期间生效。某个品类的临时规则变更也是同样的处理：新发布一个版本，并带上 effective_from。

规则文档的 front matter 声明 policy_id、scope、params 和生效窗口，**由程序确定性地解析**。正文照常由 Wiki 编译成规则页。发布一个 build 的时候，同时发布这两部分〔D16〕。

**发现的真实领域耦合（需要改动的具体位置）：**

1. `wiki_maintenance/prompts.py`：`DOCUMENT_DECISION_SYSTEM` 和 `TOPIC_PLAN_SYSTEM` 写死了「企业知识库 / 公司制度」，规则是「与制度无关的文档选择 ignore」。售后规则文档有可能被判为 ignore，措辞需要改成售后规则。
2. `wiki_runtime.py`：上传后会自动编译并**自动发布**，没有「保留为草稿，等人工审阅 diff」这一步。需要加一个 hold-as-draft 模式，并在 API 上开放 diff / publish / rollback。
3. `orchestration/wiki_schema.py`：`WikiPage` / `WikiClaim` 没有生效窗口字段。窗口放在 claim 的 metadata 里，或者放在 policy 记录里〔D16〕。
4. `wiki_maintenance/ollama_compiler.py`：模型写死为 `qwen3:4b`（TD2）。迁移方向不变，但**这不是 Stage 4 的硬性阻塞项**：正式的 eval-env-v2 使用冻结的 Wiki build 快照，compiler 用哪个模型，不会改变 Stage 4 Baseline 的正式评测输入〔D15〕。
   - **优先方案：** 如果迁移成本低，就在 Stage 4 通过 `WikiModel` 适配器接入 LLMProvider，并在 build record 里记录编译用的模型。
   - **允许延期：** 如果这项修改会带来额外的 Provider、prompt 或回归风险，就保留现在的 compiler，把它记为明确的 tech debt，延期到 Stage 5 或单独的 housekeeping。
   - **Stage 4 的硬要求只有这几条：** 售后 policy 能被编译；draft / diff / publish / rollback 能用；eval 使用冻结的 build；build 的来源和版本可以追踪。compiler 的 Provider 重构不能阻塞「售后领域迁移 + Eval Reset + Deterministic Baseline」这条主线。
5. `chat_orchestration.WIKI_PAGES` 的兜底是企业制度样例 Wiki，需要换成售后样例。
6. `rag.py` 的 `_TOPIC_NOUNS`（约第 839 行）是企业领域的名词表（年假、报销……），答案交付校验会用到它。需要**增补**售后名词。增补之后，答案交付校验族的 a 类测试必须仍然全部通过。

---

## 11. Stage 6 提前约束（只写约束，不做设计）

1. 高风险动作必须支持 `WAITING_APPROVAL` 状态，由 Guard 返回 REQUIRE_APPROVAL 时进入。
2. 需要持久化以下内容：run、pending action、args、这次执行所依据的 observations、每条相关记录的 state_version、idempotency key。
3. **审批通过不是永久通行证。** 批准之后必须：重新读取最新的业务状态 → 校验 preconditions 和 state_version → 再过一次 Policy Guard → 用 idempotency key 执行。任何一步不通过，就回到用户答复，不执行。
4. 审批被拒后，Agent 必须根据拒绝原因给用户一个明确的答复，并提供人工处理或替代方案（A23）。
5. Celery 是可选的实现手段，不是设计目标。进程内队列加持久化的 pending 表，也能满足上面这些约束。
6. **state-based eval：** 最终要比较 fixture DB 的终态和 `expected_final_state`（按表和主键逐字段比较），而不只是看回答文本。重复提交的 case 要断言只产生了一条记录。

---

## 12. 前端范围

**Stage 4 结束时，main 前后端要能独立演示。Vue 最少需要做这些：**

- **移除**「三通道模式」开关和 legacy 路径。输入框的提示文字改成售后语境。
- **演示身份：** 顶部选择 persona，列表由服务端从 seed 提供，会话级绑定。页面要明确标注「演示身份，不是登录鉴权」〔D18〕。
- **演示时钟：** 显示当前的 `virtual_now` 徽标。
- **证据面板：** 按 source_type 分组。
  - 订单和物流以卡片形式显示结构化字段（状态、签收时间、state_version、record_updated_at、observed_at）。
  - policy 证据显示规则版本和生效窗口。
  - derived 证据显示计算依据。
  - 答案里的引用编号可以跳转到对应的证据。
- **状态展示：**
  - 追问以普通助手消息的形式显示，下一轮输入照常进行。
  - 工具故障显示「数据源暂不可用」。
  - 可以只读地显示当前生效的规则 build。

**留到后面的：**

- 规则运营的 UI（草稿 diff、发布、回滚按钮）放到 Stage 5。Stage 4 用 API / CLI 就够了。
- 审批队列、WAITING_APPROVAL 状态、动作回执、终态对比视图，放到 Stage 6。

---

## 13. V1 module → V2 action

| V1 module | Action | 理由 |
|---|---|---|
| `llm_provider.py` | Adapt | 接口不变；增加 `default_temperature`，并采集 `model_reported`、`system_fingerprint` 和实际生效的参数 |
| `agent_trace.py` | Adapt | 增加 virtual_now、LLM 参数、observation_id 和故障注入标记；表结构只增字段 |
| `diagnostic_eval/` | Adapt | 归因框架保留；标签推导改为 V2 schema，增加 capability、clarification 和（Stage 6）action / final_state 阶段 |
| `eval_env/` | Adapt | make / verify / run 保留；manifest 改为 v2（seed、policy 语料、virtual_now、faults），并与 answerability 评测器解耦 |
| `eval/model_comparison.py` | Adapt | 从「换 provider」改为「换 agent control policy」（Baseline 对 Tool Loop），模型固定，control 层和端到端分开报告 |
| `check_evaluation_overlap.py` | Adapt | 相似度门禁在 V2 用来检查 dev / validation / holdout 的独立性，需要适配新 schema |
| `orchestration/contracts.py` | Adapt | 增加 business / derived 类型，以及 state_version 和 observation_id |
| `orchestration/planner.py` | Adapt | 按 §4 轻量适配后冻结为 Baseline，不做长期投入 |
| `orchestration/executor.py` | Adapt | 改为工具注册表（带 side_effect 标记），Clock 通过 context 传入，执行前经过故障注入 gateway；preflight 和脱敏机制不变 |
| `orchestration/evidence_policy.py` | Adapt | 机制保留；freshness 改为依据数据源的 freshness contract：authoritative_online 看 observed_at，snapshot 看 source_as_of，record_updated_at 不参与陈旧判断；增加业务状态冲突的判定 |
| `orchestration/system_provider.py` | Replace | 旧的 orders/inventory/approvals 被四个售后只读工具取代；安全性质要先移植 |
| `orchestration/document_adapter.py` | Adapt | 并入 search_after_sales_policy，补上 version 和生效窗口 |
| `orchestration/wiki_adapter.py`、`wiki_schema.py` | Keep | 作为 search_after_sales_policy 的内部组件保留；生效窗口放在 metadata 里 |
| `rag.py` | Adapt | 检索和交付校验保留；rerank 显式传 temperature 0；`_TOPIC_NOUNS` 增补售后名词；legacy 回答路径删除 |
| `chat_orchestration.py` | Adapt | 删除只支持 SKU 的逻辑；接入 persona、Clock 和结构化追问 |
| `api.py` | Adapt | 删除 legacy 模式；在 composition root 构造 Clock；开放规则 build 的 diff / publish / rollback |
| `storage.py` | Keep | 会话和知识的持久化与领域无关 |
| `wiki_runtime.py` | Adapt | 增加 hold-as-draft 模式，不再自动发布 |
| `wiki_maintenance/` | Adapt | 改写提示词的领域措辞；front matter 由程序解析；编译器接入 Provider 是非阻塞项，成本低就在 Stage 4 做，否则记为 tech debt 延期（§10） |
| `agent.py`（legacy decide_action） | Remove | 单域 V2 没有 legacy 路由 |
| `app.py`（Streamlit） | Remove | 前端只保留 Vue 一套 |
| `system_fixtures/*.sql` | Replace | 换成售后 base seed |
| `sample_company_rules.md`、`wiki_pages/sample_company_wiki.json` | Replace | 换成售后规则语料和样例 Wiki |
| `eval_*.json`（V1 数据集） | Remove | 在 tag 上归档，不与 V2 比较 |
| `evaluate*.py`、`run_tests.ps1`、`run_agent_evaluations.ps1` | Remove | 绑定 V1 数据集的评测脚本 |
| `check_blind_v2_schema.py`、`make_blind_v2_manifest.py` | Remove | 服务于已经污染的 blind_v2 |
| `eval/apply_label_revisions.py`、`eval/label_revisions/` | Remove | 只针对 V1 的冻结标签；overlay 的思路写进 V2 规范 |
| `eval/`（历史结果）、`eval/environments/eval-env-v1/` | Keep | 只读历史，不重跑，不覆写 |
| `docs/m10-selftest/` | Remove | M10 的一次性复现脚本，在 tag 上保留 |
| `tests/` 的 a 类 | Keep | 按 §1.3 一直保持通过 |
| `tests/` 的 b 类 | Remove | 按 §1.3 先归档再删除 |
| `frontend/` | Adapt | 按 §12 做最小改动 |
| `verify_api_sse.py` | Adapt | 按售后场景更新 SSE 验收用例 |
| `HANDOFF.md` | Keep | 追加 V2 各章节，旧内容不改 |
| `README.md`、`AGENTS.md` | Adapt | Stage 4 验收之后再改（这次不改）；AGENTS.md 的「V1 只读」约束到 Stage 6 前要改写 |

---

## 14. Stage 4 Acceptance Criteria

1. **售后领域完整运行。** 在固定的 `eval-env-v2` 里，含 `virtual_now` 的 dev 和 validation 全量都能跑完，`verify` 全部通过；运行前后 manifest 的哈希不变；没有 environment_failure。
2. **Clock 生效。** AST 静态检查和 2031 哨兵测试都通过；Trace 里每条业务证据的 observed_at 都等于 case 的 virtual_now，freshness_contract 都是 authoritative_online。
3. **Baseline 可复现。** 同一配置重复跑 3 次，生成之前各层的 Trace 哈希（去掉耗时）完全一致；答案层报告每一轮的结果和翻转数。
4. **基础设施测试全部通过。** a 类测试零失败、零新增 skip。示例数据的替换逐条记录在 HANDOFF。已有 `docs/v2/v1-test-inventory.json`。
5. **Trace / Diagnostic Eval 在新领域正常工作。** 每个 case-run 都有 Trace；诊断能把失败归到某个阶段，或明确标为 unattributed；run 和结果文件都记录 provider、model_requested / reported、temperature 等参数；正式运行的 provider 全部是 deepseek、temperature 全部为 0。
6. **Wiki lifecycle 可以用于售后规则。** 售后 policy 能被编译；至少演示一遍促销规则的草稿 → diff → 发布 → 回滚，然后 A16 / A17 在不同的 virtual_now 下得到不同的结论；eval 使用冻结的 build，build 的来源和版本可以追踪。compiler 是否接入 LLMProvider **不是**验收条件；如果没有接入，要在 HANDOFF 里记为 tech debt（§10）。
7. **评测集已建立。** 所有 case 都使用 §7.2 的固定 schema，`expected_evidence` 采用 all_of / any_of / forbidden 结构；dev 和 validation 已提交；holdout 已封存（manifest 和 sha 已提交，文件在仓库之外，路径没有出现在仓库或交给实现 Agent 的上下文里，开封脚本已提交）；相似度门禁通过；holdout 在 Stage 4 期间没有开封。
8. **没有 LLM-native Tool Loop。** 工具的选择和参数只来自确定性的 Planner。
9. **没有有副作用的动作。** runtime ToolRegistry 恰好只有五个只读工具，future actions 只出现在领域规格里；所有 case 运行前后数据库哈希不变；答案里没有「已办理」一类的说法。
10. **main 能独立演示。** 按 §12 在 Vue 上完成 A01、A03、A08、A14、A16 的演示。
11. **Baseline 已冻结。** 在首次查看 dev 结果之前打了 tag `v2-stage4-baseline` 并记录了 planner 的 sha；冻结之后的每一处修复都重新打了 tag，并在 HANDOFF 里有记录。Baseline 的 control-layer 和 end-to-end 指标分开报告。

---

## 15. 待 review 决策点

| # | 决策 | 我的建议 |
|---|---|---|
| D1 | V1 tag 的名字和位置 | `v1-final` → `3daabd0`，使用 annotated tag |
| D2 | a 类测试能不能替换示例数据 | 允许替换数据，不允许改断言语义；逐条记录 |
| D3 | `test_llm_provider_live` 在有 Key 时会联网（R11） | 保持现状，但在 CI 里要加一个显式开关；这不算「跳过」 |
| D4 | 改变 `observed_at` 的语义（从记录的 updated_at 改为读取时的 Clock） | 改；拆成 observed_at / record_updated_at / state_version 三个一等字段；freshness 依据数据源的 freshness contract（authoritative_online 看 observed_at，snapshot 看预留的 source_as_of），record_updated_at 不用来判断是否陈旧 |
| D5 | 在 Stage 4 引入确定性的派生事实 / 资格计算模块，以后由 Guard 复用 | 引入；Baseline 和 Tool Loop 共用同一个模块，对比依然公平 |
| D6 | 审计时间（Trace、storage、build）继续用墙钟 | 是，由白名单限定范围 |
| D7 | 废弃 `Route` 枚举，改为能力集合；Baseline 的 max_steps | 改；Baseline 上限定为 4 |
| D8 | Baseline 不做 observation → 参数的串联 | 不做（这是 Stage 5 的职责） |
| D9 | Baseline 仍然用 DeepSeek 生成答案；「可复现」只要求生成之前的各层 | 是 |
| D10 | 故障声明放在 `initial_state.faults` 里（schema 不增加字段） | 是 |
| D11 | holdout 放在仓库之外，而不是仓库里加混淆；隔离方式是流程 / 上下文隔离，不是权限隔离 | 放在仓库之外，路径只由人工掌握，实现 Agent 在开发期间不获知路径和内容 |
| D12 | validation 40 条，只允许按 archetype 分析；dev 和 validation 是否也由看不到实现的作者编写 | 40 条；建议 dev 和 validation 也由隔离的作者编写（V1 自编 95% 对盲测 22.5% 的教训） |
| D13 | Stage 5 用 `ask_user` 伪工具表达追问 | 在 Stage 5 定稿，但槽位词表现在就冻结 |
| D14 | Embedding 继续使用本地 Ollama | 是，按 digest 锁定在 Env 里 |
| D15 | Wiki 编译器接入 Provider（DeepSeek）还是保留 Qwen | 优先接入 Provider，但不阻塞 Stage 4：有风险就保留现状，记为 tech debt，延期到 Stage 5 或 housekeeping；不进入正式评测（使用冻结的 build） |
| D16 | 规则的结构化参数来自 front matter（由程序解析），而不是由 LLM 抽取 | front matter |
| D17 | 不单独暴露 `wiki_query`，把它并入 search_after_sales_policy | 并入 |
| D18 | demo 用服务端 persona 作为受信任身份 | 是，并明确标注不是鉴权 |
| D19 | 删除 legacy 模式和 Streamlit | 删除 |
| D20 | 给 rerank 等调用补上 temperature 0（会改变 V1 的行为） | 改；V1 已经冻结在 tag 上 |
| D21 | DeepSeek 版本怎么记录：别名、返回的 model 和调用日期；Stage 4/5 是否保持 thinking 关闭 | 三项都记录；Stage 4 保持关闭，Stage 5 再评估 |
| D22 | 业务证据和 policy 证据的 authority 数值与作用范围 | 沿用 V1 的「按作用范围生效」原则，具体数值在实现时定 |
| D23 | holdout 在 Stage 5 结束时一次性给两种 control policy 打分，Stage 4 不开封 | 是 |
| D24 | 间接注入（A13）放在 Stage 5，而不是 Stage 4 | Stage 5（Baseline 不读取 observation 里的文字） |
| D25 | 转人工在 Stage 4 只体现在回答里 | 是；escalate_to_human 在 Stage 6 实现 |
| D26 | 计日口径和时区（签收次日起算、自然日、+08:00）写进 policy params | 是，写进领域规格 |
| D27 | `after_sales_case` 现在是否预留 idempotency_key 字段 | 不预留；Stage 6 放在 pending action 表里 |
| D28 | 规则运营 UI 放在 Stage 5 而不是 Stage 4 | Stage 5 |
