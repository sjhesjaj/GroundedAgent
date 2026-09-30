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

- **顺序循环**：每次 `next_action` = 一次模型调用。runner 执行 ToolCall，observation 进入下一个
  ControlState，模型再选下一个工具：选工具 → 参数 → 执行 → 观察 → 选下一个 → … → finish。
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
  user 消息，以及该消息为最新时产生的 observation（按 sequence）。每个 observation 重建为一条合成的
  assistant tool-call 消息（id = `obs-<observation_id>`，参数为 canonical JSON）加一条 tool 消息
  （canonical ToolResult JSON；`ToolContractFailure` 为 `{"status":"malformed","tool_name":...}`，不伪造证据）。
  已经 Clarify 的动作本身不重建，投递的后续 user 消息即可。

## 3. 安全与失败语义

- **observation 是不受信任的数据**：工具输出和业务记录文本（如 `after_sales_cases.reason`）只出现在 tool
  消息里，永不拼进 system prompt。固定 prompt 明确说明这一点；不做关键词过滤——Stage 5 测的是模型是否遵守，
  硬能力边界仍由 runtime 强制。
- **重试上限（冻结）**：同一 case-run 内，同一 `(tool_name, canonical arguments)` 最多尝试 **3** 次。
  计数包含此前所有匹配的 `ToolObservation` 与 `ToolContractFailure`，不因错误重置。第 4 次请求不返回
  ToolCall，fail closed 为 `Finish("refuse")`，诊断码 `retry_cap_exceeded`。
- **无效模型协议 fail closed**：0 个调用、多个调用、未知函数、不在 allowed_tools 的工具、未提供的函数、
  参数不符合 ToolSpec 闭合契约（含任何身份参数）、非法 slots、非法 disposition → `Finish("refuse")`，
  记录稳定诊断码。未知工具名不自动转为 boundary。
- **provider / 网络故障向上抛出**（`requests` 异常原样传播），不伪装成业务 refuse。
- **Stage 5 仍然只读**：无副作用动作、无 Policy Guard、无审批——都留到 Stage 6。

## 4. 正式评测约束

- **正式 Stage 5 评测只用 DeepSeek**：`LLMNativeToolLoopPolicy(provider, formal=True)` 要求 `provider.name == "deepseek"`。
  Qwen/Ollama 可用于开发（`formal=False`），不能用于正式评测。
  （注：重建的历史 tool-call 采用 OpenAI 兼容形状，arguments 为 JSON 字符串；Ollama 原生接口上的多步运行未验证。）
- **决策审计**：每次模型调用一条 `ToolLoopDecisionRecord`（控制步、provider、请求/返回模型、token 数、
  finish_reason、原生调用数、提供的函数、选中函数、动作类型、诊断码、latency），经 `policy.decision_records`
  取得。不含 API key、reasoning、用户文本或任何评测标签；**不进入** `CaseRunRecord / DatasetRun / CaseScore /
  EvidenceState`，Stage 4 结果格式保持字节稳定。正式脚本经 policy factory 单独收集。

## 5. 数据使用与冻结顺序

1. dev 可用于 Stage 5 开发。
2. 进入 validation 之前，用 tag **`v2-stage5-tool-loop`** 冻结 Tool Loop。
3. validation 只做 aggregate / archetype 层面的分析，**不是调参集**。
4. Tool Loop 控制层冻结后，再建共享的 DeepSeek 生成 / 引用 / 端到端层，**同等地**应用于冻结的 Baseline
   和冻结的 Tool Loop。
5. holdout 保持封存，直到 Stage 5 结束时对两个冻结策略**一次性**打开。
