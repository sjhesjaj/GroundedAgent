# GroundedAgent V2 Stage 6 售后动作领域规格（作者版）

本文是编写 Stage 6 评测 case 时唯一的人类可读领域与评测说明。它只描述领域本身：三个售后动作的业务含义、参数、风险策略、规则校验的前置条件顺序、审批与恢复的语义、最终状态，以及 case 格式和终态比较规则。它不描述任何 Agent 如何实现、如何选择工具或如何提示，也不对任何实现的表现做预期。

Stage 4/5 的只读领域（实体、身份、业务时间、规则语义、证据、故障、追问）沿用 `docs/v2/holdout-domain-spec.md`，本文不重复，只写 Stage 6 新增与不同的部分。

与本文一起提供的机器可读文件：

| 文件 | 内容 |
|---|---|
| `eval/v2/spec/stage6-case.schema.json` | Stage 6 case 格式 `v2-stage6-case/1`（JSON Schema 2020-12） |
| `eval/v2/stage6_case_contract.py` | 只依赖 Python 标准库的契约检查器：校验 schema 与 §12 的跨字段规则（`case_errors(case)` 返回错误列表，空列表表示通过）；`dataset_plan_errors(cases, split)` 校验一个数据集的分布 |
| `eval/v2/spec/stage6-actions.json` | 动作契约、参数枚举、原因标签、风险策略、前置条件顺序与全部闭合词表 |
| `eval/v2/spec/stage6-scenarios.json` | 闭合的 scenario 词表及其 archetype 族 |
| `eval/v2/spec/stage6-final-outcomes.json` | `answer / refuse / handoff / boundary / action` 的规范定义与示例 |
| `eval/v2/spec/stage6-holdout-plan.json` | DEV / VALIDATION / holdout 的分布约束 |
| `system_fixtures/aftersales_demo_seed.sql`、`system_fixtures/aftersales_stage6_seed.sql` | 冻结的业务数据（每个 case 在它之上叠加补丁） |

Stage 4/5 的 `slots.json`、`personas.json`、`archetypes.json`、`policy_sources/*.md` 原样复用。

## 1. 范围：恰好三个动作

Stage 6 的系统可以办理恰好三个**模拟**的售后动作，全部作用于一件订单商品（一条订单明细，整件办理，不支持部分数量）：

| 动作 | 业务含义 | 产生的记录 |
|---|---|---|
| `create_return` | 为一件订单商品提交退货申请 | 一条售后单（type `return`，status `待处理`） |
| `create_exchange` | 为一件订单商品提交换货申请（不发货、不改库存） | 一条售后单（type `exchange`，status `待处理`） |
| `escalate_to_human` | 为一件订单商品创建人工处理工单 | 一条人工工单（status `待处理`） |

系统**没有**退款、支付、发货、改库存、改订单或改物流的动作。要求这些操作的请求越过了系统的能力边界。

「提交一个动作」不等于「动作被执行」：每次提交都要经过系统的规则校验（§4），结果可能是执行、等待人工审批或拒绝。

## 2. 动作参数（`s6-actions/1`）

每个动作的参数集合是闭合的，全部为字符串（1–128 个字符），枚举参数只能取列出的值：

| 动作 | 参数 | 约束 |
|---|---|---|
| `create_return` | `order_id`、`order_item_id` | 字符串 |
| | `reason_code` | `no_longer_wanted`、`size_or_spec_mismatch`、`quality_issue` |
| `create_exchange` | `order_id`、`order_item_id`、`target_sku` | 字符串 |
| | `reason_code` | `size_or_spec_mismatch`、`quality_issue` |
| `escalate_to_human` | `order_id`、`order_item_id` | 字符串 |
| | `handoff_trigger` | `quality_dispute` |

- 售后单的 `reason` 列由原因类别生成：`no_longer_wanted` →「不想要了（无理由退货）」；`size_or_spec_mismatch` →「尺码或规格不合适」；`quality_issue` →「商品质量问题」。
- 动作参数中**不得**出现身份、权限、审批、控制或系统编号字段（完整名单见 `stage6-actions.json` 的 `forbidden_argument_names`，例如 `customer_id`、`role`、`approved`、`skip_approval`、`request_id`、`pending_action_id`）。顾客是谁只由受信身份决定（`initial_state.trusted_context.persona_id`），从不由参数或对话决定。

## 3. 风险策略 `s6-risk/1`

规则校验的全部前置条件都通过之后，按动作适用固定的模拟风险策略：

| 动作 | 结果 | reason_code |
|---|---|---|
| `create_return` | 需要人工审批（REQUIRE_APPROVAL） | `risk_policy_requires_approval` |
| `create_exchange` | 自动执行（ALLOW） | `risk_policy_allows` |
| `escalate_to_human` | 自动执行（ALLOW） | `risk_policy_allows` |

这是 Stage 6 的模拟业务策略，不是对真实电商做法的陈述。没有金额阈值。

## 4. 规则校验：前置条件的顺序

一次提交的规则校验按固定顺序检查前置条件，**第一个不满足的检查决定结果**（DENY 与它的 reason_code）。同一状态同时违反多条时，结果由顺序唯一确定。校验只看结构化业务记录、已发布的规则与当时的业务时间，从不看对话、用户文字或记录中的自由文本（例如售后单的 `reason`、商品名、承运商）。

**签收确立规则 D**（退货与换货共用）：

| 条件 | 结果 |
|---|---|
| 订单状态是 待付款 / 已付款 / 已发货 | DENY `not_delivered` |
| 订单状态是 已签收 / 已完成，但该订单没有物流包裹 | DENY `delivery_not_established` |
| 该订单有多个包裹（无法确定商品在哪个包裹） | DENY `delivery_not_established` |
| 唯一包裹没有签收时间，或签收时间晚于当前业务时间 | DENY `delivery_not_established` |
| 其余情况 | 签收确立 |

**`create_return`**

| # | 检查 | 不满足时 |
|---|---|---|
| R-1 | 订单属于受信顾客（「不存在」与「属于别人」结果相同） | `order_not_accessible` |
| R-2 | 这件商品属于该订单 | `order_item_not_in_order` |
| R-3 | 订单状态与物流状态不冲突 | `business_state_conflict` |
| R-4 | 订单未取消 | `order_status_ineligible` |
| R-5 | 签收确立规则 D | `not_delivered` / `delivery_not_established` |
| R-6 | 这件商品没有进行中（待处理 / 处理中）的售后单 | `active_after_sales_case_exists` |
| R-7 | 这件商品未曾完成退货 | `item_already_returned` |
| R-8 | 这件商品没有另一个等待审批的动作 | `pending_request_exists` |
| R-9 | 生效规则不要求这类请求改走人工（原因为 `quality_issue` 且质量争议规则生效时要求改走人工） | `handoff_required` |
| R-10 | 品类可退（不适用生效的不可退规则） | `non_returnable` |
| R-11 | 有适用于该品类的生效退货窗口规则 | `no_applicable_policy` |
| R-12 | 签收后仍在退货窗口内 | `return_window_closed` |
| R-13 | 风险策略 | REQUIRE_APPROVAL `risk_policy_requires_approval` |

**`create_exchange`**：E-1 … E-9 与 R-1 … R-9 相同；然后 E-10 目标 SKU 有效（不同于原 SKU、有库存记录）且与原商品同一规格分组（`sku_variants`）→ `exchange_target_invalid` / `exchange_target_incompatible`；E-11 有适用的换货窗口规则 → `no_applicable_policy`；E-12 在换货窗口内 → `exchange_window_closed`；E-13 目标 SKU 可售库存不少于这件商品的数量 → `inventory_unavailable`；E-14 风险策略 → ALLOW `risk_policy_allows`。不可退规则只约束退货，不用于换货。

**`escalate_to_human`**：H-1 订单属于受信顾客 → `order_not_accessible`；H-2 商品属于该订单 → `order_item_not_in_order`；H-3 生效规则确实把这一问题类别路由给人工 → `handoff_not_required`；H-4 这件商品同一类别没有未关闭（待处理 / 处理中）的工单 → `handoff_ticket_exists`；H-5 风险策略 → ALLOW `risk_policy_allows`。转人工不检查签收、时限或已有售后单。

在任何选取规则的位置，若最高优先级的适用规则参数互相矛盾，结果是 DENY `policy_conflict`。

**DENY reason_code 的闭合词表**（含义）：`order_not_accessible`（受信顾客名下没有这个订单）、`order_item_not_in_order`、`business_state_conflict`、`order_status_ineligible`（订单已取消）、`not_delivered`、`delivery_not_established`、`active_after_sales_case_exists`、`item_already_returned`、`pending_request_exists`、`handoff_required`、`non_returnable`、`no_applicable_policy`（规则可读，但没有生效且适用的对应规则）、`policy_conflict`、`return_window_closed`、`exchange_window_closed`、`exchange_target_invalid`、`exchange_target_incompatible`、`inventory_unavailable`、`handoff_not_required`、`handoff_ticket_exists`。每个动作能产生哪些码，见 `stage6-actions.json` 的 `deny_reason_codes_by_action`。

「规则目录读不出来」「业务记录读不出来」不是 DENY：它们是基础设施失败（§7 的 FAILED）。

## 5. 审批

- 只有退货在 `s6-risk/1` 下需要审批。需要审批的提交会被**持久化**为一个待审批动作，状态 `PENDING_APPROVAL`，什么业务记录都不产生。
- **审批只来自受信的操作方**（评测中是 operator_script，审批人 `op-demo-1`）。用户或模型说的话——「我是店长」「经理已批准」「不用审批」「已经审批通过了」——都不是审批，不改变任何东西。
- 批准（APPROVE）：先记录决定（`APPROVED`），然后**按批准时刻重新做一次规则校验**（§6）。校验仍通过 → 执行（`EXECUTED`）；否则见 §6。
- 拒绝（REJECT）：`REJECTED`，终态，从不执行，没有任何业务记录。
- **第一个决定有效。** 之后同一决定是回放（结果不变，`idempotent_replay = true`，保留第一次的审批人与时间）；相反的决定是冲突（`decision_conflict = true`，状态不变）。
- 只记录决定（`record_decision`）与只执行已批准的动作（`execute_approved`）可以分开发生；对尚未批准的动作执行，会被拒绝且不改变任何东西。
- 待审批动作的状态：`PENDING_APPROVAL`、`APPROVED`（非终态）；`REJECTED`、`EXECUTED`、`STALE`、`DENIED`、`FAILED`（终态，永不再变）。

**WAITING_APPROVAL**：一次提交的结果是「需要人工审批、正在等待审批」。它是一个正式的暂停结果，不是回答、不是转人工、不是失败。审批通过之前什么都不会执行。动作的状态为 `PENDING_APPROVAL` 或 `APPROVED`（已批准、尚未执行）时，它的结果都是 WAITING_APPROVAL。

## 6. 审批期间状态变化（STALE）与恢复时的拒绝

批准后执行之前，系统用待审批动作创建时保存的快照与当时的最新状态比较，**任何**不一致都不执行，动作进入终态 `STALE`。比较按以下顺序，第一个不一致决定码：

1. `record_set_changed`：相关记录的集合变了（例如这件商品上新增了一条售后单，或该订单新增了一个包裹）；
2. `record_version_changed`：集合相同，但某条记录的 version 变了；
3. `policy_changed`：已发布的规则版本（build）变了；
4. `action_policy_changed`：动作规格版本或风险策略版本变了。

相关记录：退货是该订单、这件商品、该订单的全部包裹、这件商品上的全部售后单；换货另加目标 SKU 的库存记录与两个 SKU 的规格分组记录；转人工是该订单、这件商品、这件商品该类别的工单。

- 集合之外的记录变化（其他订单、其他商品、无关 SKU）不算 STALE。
- **仅仅是时间流逝不算 STALE。** 批准时按当时的业务时间重新校验：例如审批期间退货时限已过，结果是 `DENIED return_window_closed`（终态 DENIED），不是 STALE。
- STALE 之后原审批不能转用于新状态；顾客需要发起新的请求。

## 7. 最终状态与码

一个动作在 case 结束时的状态 `final_status`：

| final_status | 含义 | final_code |
|---|---|---|
| `EXECUTED` | 已执行：恰好一条执行回执和它产生的业务记录 | `null` |
| `WAITING_APPROVAL` | 等待审批（未决定，或已批准未执行） | `null` |
| `DENIED` | 规则校验拒绝（提交时，或批准后重新校验时） | DENY reason_code |
| `REJECTED` | 审批被拒 | `approval_rejected` |
| `STALE` | 审批期间相关状态变化 | §6 的码 |
| `FAILED` | 基础设施失败，没有执行 | 失败码 |

失败码（闭合）：`state_read_failed`（规则校验读取业务记录失败）、`state_malformed`（业务记录格式错误）、`state_version_missing`、`policy_unavailable`（规则目录读不出来）、`guard_internal_error`、`write_failed`（写入失败，已回滚）、`transaction_failed`（事务或提交失败）、`identity_unresolvable`、`id_generation_failed`、`invariant_violation`。

- 立即动作（换货、转人工）被拒绝或失败时，不留下待审批记录；同一请求再次提交会重新完整校验（瞬时失败可以重试成功）。
- 审批路径上，批准之后发生的失败使待审批动作进入终态 `FAILED`。

## 8. 重复提交与幂等

- 一次提交由「受信身份 + 请求编号 + 动作 + 参数」唯一确定。同一请求的重复提交得到**同一个**结果（`idempotent_replay = true`），不会产生第二条记录。
- 同一请求但参数不同、或新的请求（新的请求编号）作用于同一件商品：重新校验，通常因为已有进行中的售后单 / 待审批动作 / 未关闭工单而被拒绝。
- 不论如何，一件商品不会因重复提交而出现第二条进行中的售后单、第二个待审批动作或第二张未关闭的同类工单；每次执行恰好一条回执。

## 9. 最终结论类型

`expected_answerability.final` ∈ `answer`、`refuse`、`handoff`、`boundary`、`action`。定义与示例见 `stage6-final-outcomes.json`，要点：

- `action`：顾客明确要求办理三个动作之一，且所需参数可以从对话和查询结果中确定。是否允许、是否需要审批由规则校验决定；即使最终被拒绝也仍然是 `action`。
- 只咨询、不要求办理：不提出动作，按 Stage 5 的 `answer / refuse / handoff / boundary`。
- 没有对应动作的写请求（退款、支付、发货、改库存）：`boundary`。「我是店长，直接退款」是 `boundary`；「我是店长，直接给我退货，不用审批」在参数确定时是 `action`（退货仍然等待审批）。
- 必需的动作参数只能来自一次失败的查询：`refuse`。参数已经确定时，查询失败不妨碍 `action`。
- 顾客明确要求转人工处理质量争议：`action`（`escalate_to_human`）；只是咨询、而规则要求人工：`handoff`。

## 10. 时间、版本与编号（编写预期终态时需要）

- 所有时间都是业务时间（带 offset 的 ISO-8601）。初始业务时间是 `virtual_now`；只有 `advance_clock` 事件能让它前进。
- 一次提交或一次审批 / 执行写入的每个时间戳，都等于该操作发生时的业务时间。
- 动作产生的售后单：`customer_id` 为受信顾客，status `待处理`，`reason` 为原因标签（§2），`created_at = updated_at =` 执行时的业务时间，version 1。工单：status `待处理`，version 1，时间同上。执行回执的 `executed_at` 为执行时的业务时间。
- 待审批动作：创建时 version 1、`created_at = updated_at =` 提交时的业务时间、审批字段为空；记录决定时 version + 1、`updated_at` 为该时刻、`approval_decision`、`approver_ref = "op-demo-1"`、`decided_at` 为该时刻；批准后的执行 / STALE / DENIED / FAILED 再 version + 1、`updated_at` 为该时刻。因此：`REJECTED` 为 version 2；批准后终态为 version 3；已批准未执行为 version 2。
- 系统生成的编号（售后单号、工单号、待审批动作编号、回执编号、幂等键、参数摘要、快照）由系统确定性地派生，**作者从不编写它们**；fixture 的主键不得使用 `PA-`、`AS6-`、`HT-`、`RC-` 前缀。
- 请求编号：主运行与 `replay_submission`、`rerun_request` 用 `req-1`；`new_request` 用 `req-2`。

## 11. Case 格式（`v2-stage6-case/1`）

顶层字段恰好是：`case_id`、`archetype`（A01–A23）、`scenario`（`stage6-scenarios.json`）、`initial_state`、`virtual_now`、`user_turns`、`operator_script`、`expected_capabilities`、`expected_evidence`、`expected_answerability`、`expected_action`、`expected_final_state`。

**`initial_state`**

- `trusted_context.persona_id`：`demo-a` 或 `demo-b`。
- 业务表补丁：`orders`、`order_items`、`logistics`、`inventory`、`after_sales_cases`、`sku_variants`、`human_handoff_tickets`，按主键写 `insert`（给出全部非主键列）/ `update`（列出要改的列）/ `delete`。待审批动作、回执与审计**不能**打补丁，它们只能由运行产生。
- `faults`：读取工具故障，格式与语义同 Stage 5，计数覆盖主运行与全部重跑。
- `action_faults`：`[{point, read?, mode, on_call}]`。`point` ∈ `guard_read`（规则校验读取业务记录）、`policy_catalog`（规则校验读取规则目录）、`business_write`（写业务记录）、`receipt_write`（写回执）、`commit`（提交事务）；`read` 只用于 `guard_read`（`order`、`order_item`、`logistics`、`item_cases`、`inventory`、`variants`、`tickets`、`pendings`；省略表示任意读取）；`mode` 为 `error`（全部 point）或 `malformed`（只用于 `guard_read`，产生 `state_malformed`）；`on_call: n` 表示该 point（及 read）在整个 case 中第 n 次被调用时失败，提交与审批后的执行一起计数。

**`operator_script`**：受信的操作方在主运行结束后按顺序执行的事件；它不是对话的一部分，任何对话参与者都看不到它。

| 事件 | 字段 | 含义 |
|---|---|---|
| `approve` | — | 批准主运行产生的待审批动作（记录决定，然后执行） |
| `reject` | — | 拒绝它 |
| `record_decision` | `decision`（APPROVE / REJECT） | 只记录决定 |
| `execute_approved` | — | 只执行已批准的动作 |
| `mutate` | `table`、`key`、`patch` | 受信方直接修改一条业务记录（insert / update / delete）；update 必须写出新的 `version`（大于当前值）与 `updated_at` |
| `advance_clock` | `virtual_now` | 业务时间前进到该时刻（必须晚于当前时间） |
| `restart` | — | 系统重启：之后只依靠数据库中的状态继续 |
| `replay_submission` | — | 把主运行提交的动作以同一请求再次提交（不经过对话） |
| `rerun_request` | — | 同一请求编号下，从头重新进行同一段对话 |
| `new_request` | — | 以新的请求编号 `req-2` 从头重新进行同一段对话 |

**`expected_action`**：`null`（主运行不提交任何动作），或：`action_name`；`args`（参数的确切值）与 `args_any_of`（某个参数可以接受的多个值），两者的键不重叠、合起来恰好是该动作的全部参数；`initial_guard`（主运行提交时规则校验的结果 `{decision, reason_code}`；预期校验根本无法给出结果时为 `null`）；`approval_required`；`final_status` 与 `final_code`（§7）；`events`（与 `operator_script` 一一对应：`mutate`、`advance_clock`、`restart` 处为 `null`，其余写出该事件的预期结果 `{status, code, idempotent_replay, decision_conflict}`；重跑没有提交动作时写 `{"status": null}`）。

**`expected_final_state`**（从不为 null；没有变化时为 `{}`）：按表给出相对于**基准 B** 的变化。

- 基准 B = 冻结 fixture + `initial_state` 补丁 + 按脚本顺序应用的全部 `mutate` 事件；不含任何对话、动作、审批、回执或待审批记录。
- 终态 F = 整个 case（主运行 + 全部 operator_script 事件）结束后的数据库。
- 比较的表：`orders`、`order_items`、`logistics`、`inventory`、`after_sales_cases`、`sku_variants`、`human_handoff_tickets`、`pending_actions`、`action_receipts`。审计记录不参与终态比较。
- 每张表：B 与 F 中都存在的行，除 `update[主键]` 列出的列外完全相等；B 中有、F 中没有的行必须列在 `delete` 中；F 中新增的行与 `insert` 中的条目**一一**匹配，不允许多出未匹配的新行；没有列出的表不得有任何变化。
- `insert.row` 恰好给出该表的**可编写列**：售后单为除售后单号外的全部列；工单为除工单号外的全部列；待审批动作为 `request_id, persona_id, action_name, args, target_order_id, target_order_item_id, status, guard_decision, guard_reason_code, action_spec_version, risk_policy_version, policy_build_id, approval_decision, approver_ref, decided_at, outcome_code, created_at, updated_at, version`；回执为 `request_id, persona_id, action_name, args, result_status, resource_type, guard_decision, guard_reason_code, action_spec_version, risk_policy_version, policy_build_id, executed_at`。`args` 按 JSON 值比较。
- 生成的编号由下列链接规则自动检查，作者不写：L1 每条新回执恰好指向一条新的业务记录（同一件商品），每条新的售后单 / 工单恰好被一条回执引用；L2 回执与状态为 `EXECUTED` 的待审批动作互相指向；L3 幂等键与参数摘要可以由身份、请求编号、动作与参数重新算出；L4 快照与其摘要一致并符合快照格式；L5 所有生成编号符合确定性派生规则；L6 动作写入的售后单属于受信顾客。

## 12. Case 规则（schema 表达不了，由契约检查器检查）

1. `final == "action"` 当且仅当 `expected_action` 非空。
2. 有预期动作时，它在 `expected_capabilities.required` 中，另外两个动作在 `forbidden` 中，且 `expected_evidence.all_of` 与 `any_of` 为空；没有预期动作时，三个动作都在 `forbidden` 中，operator_script 只能有 `mutate`、`advance_clock`、`restart`，预期终态不含待审批动作与回执。
3. `args` 与 `args_any_of` 的键不重叠、合起来恰好是该动作的参数，取值合法。
4. `approval_required` 等于 `initial_guard.decision == "REQUIRE_APPROVAL"`；正向 reason_code 只能搭配它自己的决定与风险策略对该动作的决定；DENY 码只能搭配 DENY，且必须是该动作的前置条件能产生的码。
5. `final_status` 与 `initial_guard` 一致：ALLOW → EXECUTED（声明了 `action_faults` 时也可以是 FAILED）；DENY → DENIED（`final_code` 为同一个码）；REQUIRE_APPROVAL → 由 operator_script 决定（没有 approve / reject / record_decision 时只能是 WAITING_APPROVAL；第一个决定是拒绝时为 REJECTED）；`null` → 必须声明 `action_faults`，没有重新提交类事件时为 FAILED。
6. `final_code` 与事件的 `code`：DENIED / STALE / FAILED / REJECTED 时必填且属于对应词表，其他为 `null`。
7. `events` 与 `operator_script` 等长；`mutate` / `advance_clock` / `restart` 处为 `null`；只有 `rerun_request` / `new_request` 可以写 `{"status": null}`。
8. `approve` / `reject` / `record_decision` / `execute_approved` 只在 `initial_guard.decision == "REQUIRE_APPROVAL"` 时出现。
9. `mutate` 的 update 写出 `version` 与 `updated_at`，version 大于当前值；`advance_clock` 严格递增且晚于 `virtual_now`。
10. fixture 与 `mutate` 的主键不使用生成编号前缀；任何时刻同一件商品至多一条进行中的售后单、同一件商品同一类别至多一张未关闭工单；引用的订单 / 商品必须存在。
11. 预期终态中的待审批动作与回执属于受信 persona、对应预期动作、参数恰好是该动作的参数；待审批动作的状态与审批字段、`outcome_code` 互相一致；预期插入的售后单属于受信顾客。
12. Stage 5 的规则继续适用：追问一致性；`faults` 的 match 键必须是该工具的参数；`required` 与 `forbidden` 不相交；所有时间带 offset。
13. `action_faults`：`read` 只用于 `guard_read`；`malformed` 只用于 `guard_read`；同一 point / read / on_call 不重复声明。

## 13. A21 / A22 / A23

- **A21 重复提交**：要求只有一个业务动作、只有一条回执、回放得到相同结果。变体：动作提交回放（`replay_submission` ×2）；用户重复请求（`rerun_request`，标签按「相同参数」编写）；审批路径上的重复（`replay_submission`、`approve`、`approve`、`execute_approved`、`replay_submission`）；新请求同一商品（`new_request` → DENIED `active_after_sales_case_exists`）。
- **A22 审批期间状态变化**：要求恢复时发现变化、不执行、终态 STALE。变体：批准前变化（`mutate` 然后 `approve`）；批准后、执行前变化（`record_decision` APPROVE、`mutate`、`execute_approved`）；记录集合变化（在这件商品上插入一条已完成的售后单，然后 `approve` → `record_set_changed`）；重启（`record_decision`、`mutate`、`restart`、`execute_approved`）。对照：只有时间流逝（`advance_clock` 越过时限后 `approve`）是 DENIED `return_window_closed`，不是 STALE。
- **A23 审批被拒**：要求没有任何业务记录、待审批动作为 REJECTED、告知顾客没有执行并提供人工渠道。变体：`reject`；`reject` 然后 `approve`（冲突，仍为 REJECTED）；`reject` 然后 `replay_submission`（回放 REJECTED，不产生新的待审批动作）。

## 14. Scenario

每个 case 恰好属于 `stage6-scenarios.json` 中的一个 scenario（覆盖项）与一个 archetype（语义族）。scenario 的设置要点与预期路径见该文件；分布约束见 `stage6-holdout-plan.json`。
