# GroundedAgent V2 · Stage 5 设计稿：LLM-native Tool Loop

> 状态：Stage 5 控制层核心（PR `stage5-tool-loop-core`）。基于 `main@6da5a28`（Stage 4 已关闭）。

## 1. 对照点

- Stage 4 冻结对照：annotated tag **`v2-stage4-baseline`** → `f99d5c3`（`eval_v2.baseline.Stage4BaselinePolicy`，不再修改）。
- 两组唯一的实验变量是**控制策略**。以下全部共享、不变：
  `ControlPolicy` 协议（`next_action(state) -> ToolCall | Clarify | Finish`）、`eval_v2.runner.run_case`、
  五个只读工具、每个 case-run 一个 `FaultInjectingGateway`、证据派生（`eval_v2.evidence`）、
  Evidence Policy、scorer（`eval_v2.scoring`）、`dataset.evaluate_case / run_dataset`。
- 正式比较 `max_steps = 5`（`baseline.FORMAL_MAX_STEPS`）；`runner.HARD_MAX_STEPS = 64` 不变。

## 2. Tool Loop（`eval_v2/tool_loop.py`，`LLMNativeToolLoopPolicy`）

- **顺序循环**：每次模型决策 = 一次模型调用。runner 一次执行一个 ToolCall，observation 进入下一个
  ControlState，模型再选下一个工具：选工具 → 参数 → 执行 → 观察 → 选下一个 → … → finish。
  一个模型响应若携带原生 runtime 批次（见 §3），批次内的调用逐个占用 control step 串行执行，期间不调用模型。
- **原生 tool calling**：`provider.chat(messages, tools=..., temperature=0, max_tokens=512)`；
  动作只来自 `LLMResponse.tool_calls`。不用 JSON mode，不让模型在文本里输出 `{"tool": ...}`。
- **固定模型参数**：`temperature = 0`；thinking 由现有 provider 关闭；
  **`TOOL_LOOP_MAX_TOKENS = 512`**。不做按模型切换的 fallback prompt。
- **可见函数**：
  - 运行时工具 = `state.allowed_tools ∩ 五个 Stage 5 工具`（只会收缩）。schema 由
    `build_runtime_registry()` 的 `ToolSpec.input_schema()` 生成（经 `check_runtime_registry` 漂移校验），
    不另维护参数 schema。
  - `ask_user(slots)`：slots 枚举来自 `control.clarification_slots()` → `Clarify`（按 slots.json 顺序）。
  - `finish(disposition)`：枚举来自 `control.finish_dispositions()` → `Finish`。
  - 这两个是策略内部函数，不在 ToolRegistry，永远到不了 executor。
  - `remaining_steps == 1` 时只提供 `finish`。
- **observation → 参数链式调用允许**，且只能由模型完成（例：`get_order` 观察到 SKU → `get_inventory(sku)`）。
  策略代码不解析 observation 来填参数。
- **对话重建只用 ControlState**：一条 system 消息 = 固定 prompt + 运行时上下文
  （`virtual_now / persona_id / step_number / remaining_steps` 的 canonical JSON）；随后按投递顺序给出每条
  user 消息，以及该消息为最新时产生的 observation（按 sequence）。每个 observation 重建为一条
  assistant tool-call 消息加一条 tool 消息（canonical ToolResult JSON，取自 ControlState；
  `ToolContractFailure` 为 `{"status":"malformed","tool_name":...}`，不伪造证据）。
  已经 Clarify 的动作本身不重建，投递的后续 user 消息即可。
- **原生调用精确回放**：`llm_provider.ToolCall` 增加 `id` 与 `raw_arguments`（向后兼容）。策略实例内有一个私有
  sidecar，按产生调用的 control step 只保存线协议信封（原生 id、函数名、原始 arguments 字符串）。回放时
  assistant 消息使用原生 id 与原始 arguments，tool 消息的 `tool_call_id` 为同一 id，结果仍来自 ControlState。
  sidecar 不保存 ToolResult、证据、用户文本或任何评测信息。
  - **formal 路径**：ControlState 中存在本策略实例没有发出的运行时调用（或信封与 observation 不一致、
    或 provider 没给原生 id）时，在调用模型之前抛出 `ToolLoopProtocolError`；这是集成/协议错误，
    不是 `Finish("refuse")`，也绝不伪造 id。
  - 非 formal 的独立重建（单元测试等）可回退到确定性的合成 id `obs-<observation_id>`。
  - 预修复 smoke 中，当前 DeepSeek 端点接受了合成 `obs-*` id；改为原生 id 是协议保真 / 前向兼容加固，
    不是修复已观测到的线上故障。

## 3. 安全与失败语义

- **observation 是不受信任的数据**：工具输出和业务记录文本（如 `after_sales_cases.reason`）只出现在 tool
  消息里，永不拼进 system prompt。固定 prompt 明确说明这一点；不做关键词过滤——Stage 5 测的是模型是否遵守，
  硬能力边界仍由 runtime 强制。
- **重试上限（冻结）**：同一 case-run 内，同一 `(tool_name, canonical arguments)` 最多尝试 **3** 次。
  计数包含此前所有匹配的 `ToolObservation` 与 `ToolContractFailure`，不因错误重置。第 4 次请求不返回
  ToolCall，fail closed 为 `Finish("refuse")`，诊断码 `retry_cap_exceeded`。
- **原生多调用批次：原子接受，串行执行**（smoke：3 条「看起来可并行」的合成请求全部返回 2 个原生调用）。
  - 一个模型响应含多个原生调用时，只有**纯 runtime 批次**可被接受，且**原子**校验：每个调用都必须是已提供、
    在 `allowed_tools` 内的 runtime 工具，参数符合 ToolSpec 闭合契约（无身份参数），重试上限把同批次中更早的
    相同调用一并计入，且批次大小 `k ≤ remaining_steps − 1`（批次之后模型必须还有一步可再决策）。任一条件不满足，
    整个响应 `Finish("refuse")`，记录该成员的诊断码（批次超步数为 `batch_exceeds_step_budget`），**一个都不执行**。
  - `ask_user` / `finish` 不能与其他调用同处一个响应 → `multiple_tool_calls` → `Finish("refuse")`。
  - 被接受的批次按模型给出的顺序，每个 control step 执行一个 ToolCall，期间**不调用模型**；不重排、不过滤、
    不取舍。批次执行完后，下一次模型请求回放**一条** assistant 消息（携带批次内全部原生调用，原生 id 与原始
    arguments），随后按序每个调用一条 tool 消息。
  - **批次中途的工具失败不取消剩余调用**：模型在看到任何结果前已决定这些独立只读查询。前一调用的
    observation 无论是 OK、EMPTY、ERROR（含 timeout）还是 `ToolContractFailure(malformed)`，都只表示该调用
    完成了一次执行尝试；只要工具名、canonical 参数与 control step / 批次血缘与信封匹配，就继续执行下一个。
    不依据 status 决定是否继续。malformed 是该原生调用的合法失败 observation，不触发 `ToolLoopProtocolError`。
  - 只有结构性错误才中断（`ToolLoopProtocolError` 或 runner 的不变量）：pending 队列与 observation 不一致、
    前一调用缺少执行尝试、`allowed_tools` 硬边界被破坏、批次不完整或乱序、DB 被修改（runner 检查）等。
  - 批次排空期间不产生 `ToolLoopDecisionRecord`（记录只对应模型调用）；产生批次的那条记录的
    `batch_functions` 列出全部批次调用。
  - 不使用 `tool_choice="required"`，也不发送 DeepSeek Chat Completions 未文档化的 `parallel_tool_calls` 参数。
- **无效模型协议 fail closed**：0 个调用、含 ask_user / finish 的多调用、未知函数、不在 allowed_tools 的工具、
  未提供的函数、参数不符合 ToolSpec 闭合契约（含任何身份参数）、非法 slots、非法 disposition、批次超出步数预算
  → `Finish("refuse")`，记录稳定诊断码。未知工具名不自动转为 boundary。
- **provider / 网络故障向上抛出**（`requests` 异常原样传播），不伪装成业务 refuse。
- **Stage 5 仍然只读**：无副作用动作、无 Policy Guard、无审批——都留到 Stage 6。

## 4. 正式评测约束

- **正式 Stage 5 评测只用 DeepSeek**：`LLMNativeToolLoopPolicy(provider, formal=True)` 要求 `provider.name == "deepseek"`。
  Qwen/Ollama 可用于开发（`formal=False`），不能用于正式评测。
  （注：重建的历史 tool-call 采用 OpenAI 兼容形状，arguments 为 JSON 字符串；Ollama 原生接口上的多步运行未验证。）
- **诊断分类报告规则**：正式 Stage 5 DEV 报告必须从 `ToolLoopDecisionRecord`（经评测 harness 捕获的策略实例）
  分开计数至少以下几类，以区分「任务/控制失败」与「模型原生协议遵从失败」：
  `no_tool_call`；`multiple_tool_calls`；`retry_cap_exceeded`；其余模型协议诊断码（合计并分码列出）；
  provider / 协议执行错误（HTTP / 网络异常、`ToolLoopProtocolError`，由 harness 记录）。
  另报告被接受的原生批次数量与大小（`batch_functions`）。不为此修改 `DatasetRun / CaseScore`。
- **决策审计**：每次模型调用一条 `ToolLoopDecisionRecord`（控制步、provider、请求/返回模型、token 数、
  finish_reason、原生调用数、提供的函数、选中函数、批次函数、动作类型、诊断码、latency），经 `policy.decision_records`
  取得。不含 API key、reasoning、用户文本或任何评测标签；**不进入** `CaseRunRecord / DatasetRun / CaseScore /
  EvidenceState`，Stage 4 结果格式保持字节稳定。正式脚本经 policy factory 单独收集。

## 5. 数据使用与冻结顺序

1. dev 可用于 Stage 5 开发（迭代协议见 §6）。
2. 进入 validation 之前，用 tag **`v2-stage5-tool-loop`** 冻结 Tool Loop。
3. validation 只做 aggregate / archetype 层面的分析，**不是调参集**。
4. Tool Loop 控制层冻结后，再建共享的 DeepSeek 生成 / 引用 / 端到端层，**同等地**应用于冻结的 Baseline
   和冻结的 Tool Loop。
5. holdout 保持封存，直到 Stage 5 结束时对两个冻结策略**一次性**打开。

## 6. Stage 5 DEV 迭代协议

Stage 5 与 Stage 4 的冻结策略不同：**DEV 是 Stage 5 Tool Loop 的开发 / 调试集**。

### 6.1 开发预算：最多 3 个有效 DEV 轮次

- **DEV Round 1**：PR 首次 merge 后的 Tool Loop。
  - 允许依据 DEV 诊断修改：Tool Loop 协议集成；prompt / 工具使用说明；控制策略；参数与追问（clarification）行为。
  - 不得：修改数据集或 expected 标签；按 case-id / archetype 特判；把具体 DEV 句子写入规则或测试；
    查看 validation 的单条 case；查看 holdout。
- **DEV Round 2**：记录 Round 1 → Round 2 的代码 / prompt 变化与指标变化。
- **DEV Round 3**：最后一个开发候选。
- Round 3 完成后：**不再做任何基于 DEV 的调参**。创建 tag **`v2-stage5-tool-loop`** 冻结，随后运行 VALIDATION。

### 6.2 什么算一个有效 DEV 轮次

只有完整跑完全部 DEV dataset，且同时满足以下条件，才计入 3 个轮次：

- 没有基础设施崩溃；
- 数据库不变量成立；
- 数据集字节未变；
- control / eval 契约有效。

因崩溃、provider 集成 bug、DB 被修改、序列化 / harness 契约 bug 而无效的运行，记为
**INVALIDATED DEV RUN**，不是有效评分轮次。允许修复以使评测恢复有效，但必须记录：

- 无效运行的原因；
- 修复 commit；
- 声明没有利用该无效运行做业务规则调参。

不得用「无效运行」变相扩充普通调参次数。

### 6.3 Validation 冻结规则

- `v2-stage5-tool-loop` tag 必须在 validation **之前**创建。
- Validation 期间：不修改 Tool Loop、不修改 prompt、不修改工具协议；只报告 aggregate 与 archetype 层面；
  不针对单条 validation case 调优。
- 任何 validation 分数下降都**不能**重新打开 Tool Loop 开发。

### 6.4 Holdout

- holdout 保持 sealed，直到 Stage 5 最终一次性打开。
- 届时并排评分：冻结的 Stage 4 Baseline（`v2-stage4-baseline`）与冻结的 Stage 5 Tool Loop（`v2-stage5-tool-loop`）。
- 不得根据 holdout 修改任何控制策略。

### 6.5 共享 generation 仍属于 Stage 5

- Tool Loop 控制策略冻结后，还要实现共享的 DeepSeek generation / citation / E2E 层，
  以完全相同的方式应用于冻结的 Stage 4 Baseline 与冻结的 Stage 5 Tool Loop。
- generation 层的开发只能使用 DEV / 规格说明，不得根据 validation 或 holdout 的单条 case 调优。
- holdout 打开之前，generation / evaluator 也必须冻结。
