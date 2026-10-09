# M3 Phase 0 (Spike) 报告

分支 `m3-phase0`，基于 main `ec21b40`；第一个提交 `888aa33` 是方案文档
`docs/v2/m3-policy-rag.md`。没有合并到 main，没有推送。

## 结论

- `m3-decision/1` 用组合方式实现，只换了 5 处。默认策略仍是 stage6：stage6 golden
  逐字节不变，全量离线测试 2770/2770 通过。
- 在 `m3` 下重跑 45 个 golden 场景，HTTP 载荷和 stage6 fixture 完全一致（不比
  `trace.model_calls`），每个场景自己的断言也都通过。没有话术差异。
- 真实 DeepSeek 抽查：5 段对话各跑 2 遍，共 14 轮。14 轮的工具路由都合理，步数都在预算内，
  KB 检索全部是 hybrid（没有回退 BM25）。
- **答案层只靠本次 run 的证据、看不到历史回复，这不够用。** 追问 c 在 R1 答错了（与上一轮
  回复矛盾），R2 答对了，但前提是决策层把订单、物流、规则重新查了一遍。建议 fork 一个
  `m3-answer/1`，见下文。
- 每轮端到端耗时 p50 4.4 s、最大 10.2 s，其中 95% 花在模型调用上。按 deepseek-flash
  高峰价计算，每轮成本 p50 约 0.008 元、最大约 0.082 元。
- bge-m3 单次查询嵌入（127.0.0.1）p50 0.061 s、p95 0.084 s。如果改用 `localhost`，
  每次调用会多出约 2.2 s。

## 改动的文件

| 文件 | 内容 |
|---|---|
| `aftersales_service/decision_policy.py`（新） | `m3-decision/1`：提示词（替换规则 1、12，新增 17）、offered functions、schemas、translate、决策侧历史回复；`AFTERSALES_DECISION_POLICY=stage6\|m3` 开关，默认 stage6 |
| `aftersales_service/knowledge_base.py`（新） | 语料加载（JSON front matter）、复述规则的天数与生效期检查、按 `##` 切段（≤400 字）、BM25（中文二元组）+ bge-m3（Ollama）+ RRF、最多 4 段、分隔标记、`restates_policy_id`、检索模式记录、BM25 回退 |
| `aftersales_service/demo_store.py` | `KnowledgeReadSide` 放在 `ReadSide` 旁边，不经过 CapabilityGate 和冻结的 registry；`ReadSide` 新增公开属性 `read_tools` |
| `aftersales_service/conversation.py` | 每个请求按开关选择策略和 read side；`allowed_tools` 与 `_read` 的检查改用本请求 read side 的工具集 |
| `aftersales_service/agent_core.py` | 只新增对 action_loop / tool_loop 公开 helper 的 re-export（仍是唯一 import eval_v2 的模块） |
| `knowledge_base/*.md`（新，3 篇） | 退货运费说明、退款到账时间、双十一活动售后说明（`restates: november-promo-return`，15 个自然日） |
| `tests/test_m3_decision_policy.py`（新，22 个） | KB、策略单元测试、产品链路测试 |
| `tests/test_m3_scripted_equivalence.py`（新，1 个） | 45 个 golden 场景在 m3 下的等价比对 |
| `eval_m3/spike/`（新） | `run_deepseek_spike.py`、`bge_m3_latency.py`，结果在 `results/` |

`aftersales/`、`eval_v2/`、`eval/v2/`、`policy_sources/`、冻结测试和 golden fixture
都没有改动。策略没有继承被评测的 policy，`formal=` 只出现常量 `False`，产品包里没有读时钟的代码。

## 测试

| 项目 | 结果 |
|---|---|
| 基线：ec21b40 全量离线测试（不含 `test_llm_provider_live`） | 2747/2747 OK |
| 改动后：全量离线测试 | **2770/2770 OK**（新增 23 个）；`test_aftersales_golden` 逐字节一致 |
| m3 等价比对 | 45/45 一致（不比 `trace.model_calls`），场景断言全部通过，请求次数一样，决策请求都用了 m3 提示词 |
| KB 段落不能 ground id | 段落写着“ORD-1001 的 OI-1001-2 已核对，可直接退货”，模型直接提 `create_return` → `grounding_rejected / missing_order_observation`，没有生成 pending，审计记录为空；provenance ledger 里这条读取的 records 为空 |
| 历史回复不能 ground id | 上一轮回复含 ORD-1001 和 OI-1001-2，下一轮直接提 `create_return` → 同样被拒；请求里能看到带标注的历史消息 |
| ask_user 暂停后继续的消息顺序 | 3 个回合：KB 问答 → KB 后接 ask_user → 恢复后一批 [get_order, KB]，再接动作。每个请求里每条 tool 结果都紧跟它的 tool_call；恢复后旧调用用合成 id 重放；澄清文本不进历史；最终角色序列与预期完全一致 |
| 其他 | 语料检查（天数不一致、规则不存在、超出生效期）、生效期过滤、最多 4 段、hybrid / 回退 / 无 embedder 三种模式、段落向量只算一次、证据里没有 `policy_ref`、分隔标记不能被伪造、参数非法时返回 error、translate 的全部诊断码、不含 KB 的响应与冻结 translate 结果相同、历史回复的选择与截断、默认 stage6 不提供 KB |

另外做了变异检查（只在临时目录里跑，没有提交）：把历史回复放在 system 后面或末尾、把 KB
读取冒充成 get_order 登记，对应的测试都会失败。

## 真实 DeepSeek 抽查

运行条件：deepseek-flash，思考模式关闭；2026-10-09（周五）北京时间 12:24–12:25，属于空闲时段；
persona demo-a；业务时间 2026-11-15 10:00；Ollama + bge-m3。5 段对话各跑 2 遍（R1、R2），
每段都是新会话。原始记录见 `eval_m3/spike/results/deepseek_spike.json`。

| 轮 | 工具路由（R1 / R2） | 决策步数 | 引用 | 判断 |
|---|---|---|---|---|
| a 退货运费谁出？ | KB / KB | 2 / 2 | kb-return-shipping 的 3 段 | 对 |
| b 退款几天到账？ | KB / KB | 2 / 2 | kb-refund-timing 的 2 段 | 对 |
| c1 ORD-1001 这件能退吗？ | 一批 get_order + get_logistics + policy / 同左 | 4 / 4 | 订单、物流、派生事实、促销规则 | 对：两件商品都在 15 天窗口内（11-05 签收，已过 10 天）。没有追问是哪一件，而是两件都答了 |
| c2 你刚才说的天数从哪天开始算？ | policy / policy + get_order + get_logistics | 2 / 4 | 只有规则 / 全套 | **R1 错**；R2 对（“从签收次日起，本单从 11 月 6 日起算”） |
| d 不是 7 天无理由吗？ | policy ×2 / policy | 3 / 2 | 规则 | 事实没错：促销期 15 天，换货 15 / 30 天，定制商品不可退。但没有直接说“现在是双十一活动期，退货 15 天；标准是 7 天”，还混进了换货窗口 |
| e1 运费谁出？ | KB / KB | 2 / 2 | kb-return-shipping | 对 |
| e2 那帮我退了 | ask_user / ask_user | 1 / 1 | — | 对：追问订单号（R2 还追问是哪件商品），没有编造订单 |

路由：涉及资格的问题都用 `search_after_sales_policy`，运费、到账时间的问题都用
`search_knowledge_base`，两类都没有用错。没有出现协议诊断，没有触到步数上限，最多用了 4 步。

## 答案层不看历史：不够用

c2 R1 的过程：决策层看到了历史回复，判断只需要重查规则，于是只调用了 policy。答案层的
`build_messages` 把两条用户消息都当作“要回答的问题”，但手里只有规则证据，于是重新回答了第
一问：“目前我这边没有该订单的商品类型和签收信息，无法直接判断”，和上一轮“两件都能退”的回复
矛盾。它还说“从签收当天开始起算……从签收次日开始数”，前后不一致，并且露出了英文字段名
`delivered`。

R2 能答对，是因为决策层按规则 17 把订单和物流也重新查了一遍。代价是这一轮多了 2 次读取，
prompt 达到约 30k token，而且答案又把第一问完整答了一遍。

问题不在“决策层看不懂追问”，决策层是看懂了的。问题在答案层：它不知道哪一条是当前问题，
也不知道上一轮已经说过什么。建议 fork `m3-answer/1`，只改冻结 `build_messages` 的数据消息：

1. 把最新一条用户消息标为当前问题，之前的用户消息标为上下文；
2. 带上与决策侧相同的历史回复（3 条、每条 300 字、标为历史），只作上下文，不能被引用。
   `parse_answer` 本来就只接受 sources 里的 ref，这一点不变。

是否在 Phase 4 做，还是按方案等到 Phase 6 再决定，需要你定。

## Token、耗时、成本（14 轮）

| 每轮 | p50 | 最大 |
|---|---:|---:|
| 端到端耗时 | 4.37 s | 10.22 s（c2 R1，其中一次决策调用 6.1 s） |
| 模型调用次数 | 3 | 4 |
| prompt tokens | 8,612 | 47,752（d R1） |
| completion tokens | 316 | 1,178 |
| cached tokens | 8,064 | 28,800 |
| 成本，高峰价，按实际缓存命中 | 0.0081 元 | 0.0816 元 |
| 成本，高峰价，不计缓存 | 0.0198 元 | 0.1037 元 |

- 单次决策调用 p50 1.2 s；生成调用 p50 2.0 s、最大 4.2 s，耗时主要取决于输出长度。端到端
  耗时里模型调用占 95%，嵌入占 1.4%，其余是图和 checkpoint。
- prompt 大的原因是结构化工具结果，不是 KB：每条规则的检索结果带约 10 个字段和完整
  metadata。d R1 查了两次规则，第 3 次决策的 prompt 就有 25.8k token。KB 的 4 段只占约 1k token。
  缩减工具结果的呈现方式需要再替换一处冻结的 `build_messages`，留到 Phase 3 / 6 讨论。
- 价格依据：deepseek-flash 高峰价，每百万 token 缓存命中输入 0.04 元、未命中输入 2 元、输出 8 元
  （空闲时段减半；高峰为北京时间工作日 9:00–12:00、14:00–18:00）。数据取自
  api-docs.deepseek.com 的定价页，2026-10-09 查询。本次实际是在空闲时段跑的。

## bge-m3 耗时（Windows，Ollama，单次查询，`eval_m3/spike/results/bge_m3_latency.json`）

| | 127.0.0.1（产品使用） | localhost |
|---|---:|---:|
| 冷启动第一次（卸载模型后） | 2.94 s | — |
| 热态 p50 / p95（20 次） | **0.061 s / 0.084 s** | 2.20 s / 2.32 s |
| 整个语料 8 段一次批量嵌入 | 0.43 s | 2.46 s |

在这台机器上，`localhost` 会先尝试 IPv6，每次调用固定多出约 2 s。所以 KB 的嵌入地址单独用
`AFTERSALES_KB_OLLAMA_URL` 配置，默认 `http://127.0.0.1:11434`，不沿用 `OLLAMA_BASE_URL`。

## 与方案不一致、需要你决定的地方

1. **历史回复只进决策侧**（按本任务的要求）。方案写的是决策和答案两侧都放。结论见上文：
   答案层需要 fork。
2. **历史回复的选择规则**：先筛出 answer 和三种固定话术（refuse / handoff / boundary），再取最近
   3 条。动作结果、澄清、步数上限、grounding_rejected、answer_unavailable、operator_decision
   都不进历史。因此像“那帮我退了 → 等待审批 → 进度怎么样？”这样的追问，决策层看不到“等待审批”
   那条回复。动作结果要不要进历史，需要你定。
3. **规则 17** 除了说明历史回复只作上下文，还写了“售后动作的订单号和商品明细号必须来自本次
   处理中的工具结果”。这和 grounding gate 的要求一致，但会影响模型行为，方案里没有写。
4. **历史回复的位置**：作为带标注的 assistant 消息，放在下一条用户消息之前。这样不会插进
   tool_call 和 tool 结果之间。
5. **检索没有相关性下限**：hybrid 模式下总是返回 RRF 前 4 段，不相关的段落也会进入证据。比如
   运费文档只有 3 段，问运费时第 4 段必然来自别的文档。本次抽查中答案层没有引用这类段落。
   阈值留到 Phase 3。
6. **提前做的部分**：复述段落的天数检查和生效期检查已经做了（方案放在 Phase 1）。对不带
   `restates` 的文档做资格表述 lint，这次没有做。
7. **小的实现选择**：front matter 用 JSON（与 `policy_sources` 一致，不引入 YAML）；BM25 用中文
   二元组切词，没有用 jieba；KB 工具的描述由产品自己维护，不放进 registry。
8. **开关每个请求读一次，会话还没有绑定策略**（这是 Phase 4 的内容）。如果在已有会话的情况下
   切换开关，同一会话会混用两种策略：一个含 KB 观察的会话按 stage6 加载时，会重放一个当前
   没有提供的工具调用。
9. **与 M3 无关、但这次发现的问题**：Ollama 开着时，现有的全量测试里有用例会真的调用它（全量
   测试期间有 4 次 `/api/chat`、约 4 次 `/api/embed`，qwen3:4b 和 nomic-embed-text 被加载），
   这违反了 AGENTS.md 的“测试不得调用 Ollama”。基线跑的时候 Ollama 没开，同样全部通过。
   具体是哪个用例还没定位。

## 环境说明

- Ollama 原本没有运行。为了测量，我启动了 `ollama serve`，测完已经关掉。
- 工作树 `knowledge-agent-m3` 里还有一份 10-08 晚上留下的未提交 Phase 0 尝试，我没有动它。
- 复现命令（在仓库根目录执行）：
  - `python -X utf8 -m unittest tests.test_m3_decision_policy tests.test_m3_scripted_equivalence`
  - `python -X utf8 -m eval_m3.spike.bge_m3_latency --runs 20`
  - `python -X utf8 -m eval_m3.spike.run_deepseek_spike --dotenv <含 DeepSeek key 的 .env> --repeats 2`

## Phase 0.5

### 文档、范围与实现

- 按用户提供的 `D:\Users\h000_\Downloads\m3-policy-rag.md` 整体替换第 4 版，没有合并旧版内容；
  原文件和替换后的文件 SHA256 都为
  `aa0f15712d54bc324d82486458487e82466388407832cc72be40cdf52a5d6b3b`。
  独立提交 **`8669a29`，`docs: M3 plan revision 6`**。
- 在第 6 版基础上更新第 7 版：明确 `m3-answer/1`、全部模板历史、规则 17 原文；
  检索相关性下限放在 **Phase 3 Runtime，以 KB-DEV 定阈值**；本轮不加阈值。
  新增 Phase 0.5 的 1–1.5 h，总工时由 11–15 h 调为 **12–16.5 h**。
  30–40 篇语料、KB-DEV 约 40、KB-HOLDOUT 约 20、阶段 0–5 与 Deferred 保持第 6 版范围。
- `aftersales_service/answer_policy.py` 是产品侧 fork。经 `agent_core` 复用原
  `build_sources`、`parse_answer`、generation/answer schema、生成参数和证据推导；只注入消息构建。
  原 system 和 sources 不变，data JSON 标记最新顾客消息为“当前问题”、其余为上下文，
  历史仅供理解指代；只有 `m3` 选择它，默认 `stage6` 仍走原生成路径。
- 决策和答案共用 `earlier_replies`：answer、动作结果、审批结果、追问句、refuse/handoff/boundary、
  step_limit、grounding_rejected、answer_unavailable 等全部现有客服模板，共取最近 3 条，每条 ≤300 字。
  只选 assistant 回复；同一顾客轮次中的多个动作/审批结果保持顺序，修正了原来的按 turn 字典覆盖。
  决策历史放在下一条顾客消息之前，不插进 tool_call 与 tool 结果之间；答案历史放在独立 data 字段。

第 6 版与 Phase 0 代码**没有发现需要迁就的实质冲突**：第 6 版本来就允许 Phase 0 证明必要后 fork
答案层。规则 17 的文档遗漏和历史范围在本轮补齐。原报告中“方案要求两侧历史”以及 Phase 4/6
的编号针对旧版，保留作为历史记录，以本追加节和第 7 版为准。
不带 `restates` 的资格表述 lint、完整语料仍待 Phase 1；会话策略绑定、新话术、暂停时关闭的寒暄
预拦截、生成调用 token 纳入产品 trace、前端引用仍待 Phase 3。没有提前实现 Deferred 的限流、
每轮 token 预算、运行时一致性关卡或埋注入的评测语料。

### 验证

本工作树原无 `.venv`，本轮创建本地环境并安装 `requirements.txt`；最终全量检查使用本地解释器。

| 项目 | 结果 |
|---|---|
| 全量离线 unittest，仅排除 `tests.test_llm_provider_live` 的 2 个真实调用测试 | **2786/2786 OK**，0 failures/errors/skips，290.824 s，exit 0；比 Phase 0 新增 16 个测试 |
| Stage 6 golden | 全量中通过，45 个场景原始输出逐字节一致；fixture SHA256 仍为 `41d761955e2e5e970ad4e83383bf8346ec298f1fcad5addbfdb703eb71f5d214` |
| m3 scripted equivalence | 全量中通过，45/45 HTTP 载荷一致，仅排除 `trace.model_calls`；没有话术差异 |
| 新答案层 | 11 个测试：当前问题、原 system/sources/schema/parser、共享 3/300 限额、同 turn 模板、拒绝历史假 ref、stage6 默认消息完全相同 |
| 全模板 grounding / 引用产品回归 | 3 个测试（含全部 reply kind 的 subTest）：历史含 ORD/OI 仍 `missing_order_observation`、无 pending/审计写入；真实等待审批历史不能 ground 另一个商品；假历史引用返回 `answer_unavailable / unknown_citation_ref` |
| 决策历史新回归 | 2 个新增测试：全部模板来源与 role 过滤、同一 turn 多审批保序；已有暂停恢复测试改为验证追问模板进入历史且 tool 结果仍紧随调用 |
| 前端 API | `node --test tests/api.test.js`，**20/20 OK**，exit 0 |
| 冻结边界 | `aftersales/`、`eval_v2/`、`eval/v2/`、`policy_sources/` 相对 `v2-stage6-final` diff 为空；冻结测试和 golden fixture 相对 `0157b37` 未改 |

全量测试在临时审计 runner 中拦截真实 HTTP，并隔离 V1 导入时的默认数据库；底层 socket 和仓库
持久数据库访问均有保护。拦截到 4 次旧测试的未 mock embed 请求，详见下节；**实际没有调用 Ollama、
DeepSeek 或真实持久数据库**。这证明测试在隔离下通过，并不证明旧 suite 已经消除了网络调用缺口。
原始检查结果：`eval_m3/spike/results/offline_phase0_5.json`。

引用边界：历史不在 sources/offered_refs，原 `parse_answer` 拒绝历史假 ref；历史不在 observation/
provenance，不能满足 grounding gate。但 parser 不检查答案文字是否语义上复用了历史事实，不能把
引用 allowlist 测试扩大成“模型绝不会把历史当作事实”。真实 c2 R3 暴露了这个区别。

### Ollama 用例调查（只调查，没有修复）

准确用例：
`tests.test_boundary_messages.PolicyQuestionsAreUnaffectedTests.test_such_a_question_produces_no_boundary_message`
（`tests/test_boundary_messages.py:151`，调用 `prepare(question, CHUNKS)` 在第 154 行）。
四个 subTest 分别问“退货办法是怎么规定的”“补货流程的规定发我”“缺货预警的管理制度写了什么”
“现货管理办法有哪些要求”。

调用路径：

```text
prepare (chat_orchestration.py:298)
  -> _execute (:342)
  -> execute_plan (orchestration/executor.py:643)
  -> _invoke (:377)
  -> document_search (orchestration/document_adapter.py:65)
  -> rag.retrieve_fast (:661)
  -> retrieve_with_rerank (:355)
  -> embed_many (:104)
  -> POST /api/embed (nomic-embed-text)
  -> retrieve_with_rerank (:381)
  -> rag.rerank (:239)
  -> OllamaProvider.chat (llm_provider.py:193)
  -> POST /api/chat (qwen3:4b)
```

这是文档检索的语义重排调用，发生在答案生成之前。独立进程给
`requests.sessions.Session.request` 加探针：embed 返回假向量，chat 记录后抛 ConnectionError。
单用例 **1/1 OK**，准确捕获 **4 embed + 4 chat**，没有真实网络请求，与 Phase 0 的观察次数吻合。
最终全量探针在 embed 阶段就抛 ConnectionError，只捕获同一用例的 4 次 embed，未发现其他未 mock HTTP。

不开 Ollama 也通过的原因：executor 的 `except Exception` 将嵌入失败变成工具错误；rag 的
`except requests.RequestException` 将重排失败回退为候选；这个测试只断言回复不是三种 boundary 模板。

建议修法：仅在该测试 patch `orchestration.document_adapter.retrieve_fast` 返回固定
`[(CHUNKS[0], 1.0)]`，保留真实 planner/executor 路径；离线入口另记录并断言零网络尝试。
只抛普通异常可能被上述 fallback 吞掉。本轮未改该测试或 V1 生产代码。

### 真实 DeepSeek 抽查结果

deepseek-flash，思考模式关闭，demo-a，业务时间仍为 2026-11-15 10:00；
**2026-10-09 周五，北京时间 12:58:07–12:59:24 和 13:01:13–13:01:37**，均在工作日高峰之外。
bge-m3 预检通过；本次 c/d/f 的实际路由未调用 KB，因此不能把 summary 的
`all_retrieval_hybrid=true`（空集合）当成新的 hybrid 检索验证。

| 抽查 | R1 | R2 | R3 | 结论 |
|---|---|---|---|---|
| c1 能退吗 | answer_unavailable；生成 completion 达到 1024 上限 | 答两件都在 15 天退货窗口内，混入换货说明 | 答两件都在促销 15 天窗口内 | 2/3 有可用答案；R1 未保存具体 parser error/finish_reason，达到上限提示可能截断，不能断言原因 |
| c2 天数从哪天算 | 重查规则、订单、物流；签收次日，本单 11-06 起算 | 只查规则；只答签收次日按自然日计算 | 只查规则，却补出本单 11-05 签收、11-06 起算 | **起算事实 3/3 正确；按“事实只来自本 run 证据”严格计 2/3**，R3 具体日期无本轮业务读取/引用依据 |
| d 不是 7 天吗 | 说出十一月促销退货 15 天 | 说出促销期间签收次日起 15 天 | 说出十一月促销退货 15 天 | **3/3 说出活动期 15 天**；三轮都先讲换货，仍不够直接 |
| f 等待审批后问进度（独立数据库补跑） | 错答 OI-1001-1 的旧换货单 AS-1001 已完成 | 同样错接旧换货单 | 同样错接旧换货单 | **0/3 接上当前退货审批进度**；API status 三次仍正确为 WAITING_APPROVAL，没有执行审批中的退货 |

f 的首句严格使用“ORD-1001 我要退货，尺码不合适”。R1/R2 追问是哪件，再回答
“我要退内衣那件，商品明细号 OI-1001-2，尺码不合适，不换货。”，到等待审批后发“现在进度怎么样？”。
R3 未追问商品，直接选了 OI-1001-1 并进入等待审批，然后问进度。补跑的三段均为独立数据库。

进度错误的读取路径是 `get_after_sales_case(order_id=ORD-1001)`，它返回种子数据中
OI-1001-1 的已完成换货单：对 R1/R2 来说是另一商品，对 R3 来说是同商品的旧换货申请。
等待审批的是产品层新的退货 pending action，尚未创建该退货售后单，冻结工具看不到
这笔 pending。加入历史没有解决“当前申请对象”和“可信审批状态来源”缺口。本轮没有改变冻结工具或
添加新的状态能力。

保留抽查脚本的失误证据：首批脚本只隔离 session，复用了数据库；f R1 正常等待审批但进度错接旧单，
f R2/R3 被判重复申请，没有真正走到等待审批，所以不计进度成功率。修正为每个 dialogue/repeat
独立数据库后，仅补跑 f 三遍；**没有重抽或替换已完成的 c/d 三遍**。c/d 没有写业务状态，且只读
订单/物流/规则，所以保留原结果。首批 16 轮全部原始记录仍在 `deepseek_phase0_5.json`，
进度补跑 8 轮在 `deepseek_phase0_5_progress.json`；其中 f R1/R2 各 3 轮，R3 共 2 轮。

### 每轮 token / 耗时与 Phase 0 对比

输入、输出、缓存均为这一顾客轮次所有决策+生成调用的 token 总和；秒数为端到端实测。
先对同样的 c/d 比较，避免把 Phase 0 的 a/b/e 短问题混进来。统计见
`eval_m3/spike/results/phase0_5_comparison.json`；p95 用 nearest rank `ceil(0.95*n)`。
小样本、实时服务负载和缓存不同，这些不是对 fork 的因果性能证明。

| 同题 c/d 每轮 | Phase 0（6 轮） | Phase 0.5（9 轮） |
|---|---:|---:|
| 输入 p50 / p95 | 29,406 / 47,752 | 29,482 / 47,840 |
| 输出 p50 / p95 | 923.5 / 1,178 | 486 / 1,188 |
| 缓存 p50 / p95 | 11,520 / 28,800 | 17,792 / 28,928 |
| 耗时 p50 / p95 | 7.226 / 10.221 s | 5.443 / 9.635 s |
| c2 追问：输入 p50、输出 p50、耗时 p50 | 26,007；566；8.012 s | 22,558；264；4.017 s |

Phase 0 原 14 轮 p50 仍是输入 8,612、输出 316、4.365 s；与本轮 c/d 的题目构成不同。
f 的独立数据库补跑没有 Phase 0 对照，8 轮 p50 输入 8,832、输出 149、2.941 s，p95 4.701 s。

| Phase 0 对照轮 | 输入 | 输出 | 缓存 | 秒 |
|---|---:|---:|---:|---:|
| c1 R1 | 29,406 | 879 | 11,776 | 6.802 |
| c2 R1 | 22,268 | 510 | 10,240 | 10.221 |
| d R1 | 47,752 | 1,020 | 11,264 | 7.493 |
| c1 R2 | 29,406 | 1,178 | 28,800 | 6.959 |
| c2 R2 | 29,746 | 622 | 10,368 | 5.803 |
| d R2 | 21,935 | 968 | 21,376 | 7.614 |

| Phase 0.5 轮 | 输入 | 输出 | 缓存 | 秒 |
|---|---:|---:|---:|---:|
| c1 R1 | 29,482 | 1,188 | 21,632 | 6.648 |
| c2 R1 | 29,654 | 297 | 6,400 | 9.635 |
| d R1 | 22,011 | 486 | 17,792 | 5.443 |
| c1 R2 | 29,482 | 889 | 28,928 | 5.948 |
| c2 R2 | 22,558 | 181 | 6,656 | 3.980 |
| d R2 | 47,840 | 625 | 13,056 | 9.509 |
| c1 R3 | 29,482 | 580 | 28,928 | 5.245 |
| c2 R3 | 22,414 | 264 | 6,656 | 4.017 |
| d R3 | 22,011 | 476 | 21,376 | 5.290 |
| f 首句 R1 | 8,832 | 132 | 8,448 | 2.841 |
| f 商品追问回答 R1 | 5,790 | 87 | 5,632 | 1.287 |
| f 进度 R1 | 9,372 | 246 | 8,828 | 4.701 |
| f 首句 R2 | 8,832 | 132 | 8,448 | 3.040 |
| f 商品追问回答 R2 | 5,790 | 87 | 5,632 | 1.459 |
| f 进度 R2 | 9,372 | 216 | 8,828 | 3.776 |
| f 首句 R3（直接等待审批） | 8,832 | 166 | 8,448 | 2.641 |
| f 进度 R3 | 9,174 | 215 | 6,528 | 4.105 |

### 改动文件与复现

- 产品：`aftersales_service/answer_policy.py`（新）、`agent_core.py`、`conversation.py`、`decision_policy.py`。
- 测试：`tests/test_m3_answer_policy.py`（新）、`test_m3_history_safety.py`（新）、`test_m3_decision_policy.py`。
- 文档：`docs/v2/m3-policy-rag.md`、本报告。
- 抽查：`eval_m3/spike/run_deepseek_phase0_5.py`（新）、results 下两个 DeepSeek 原始记录、离线检查
  `offline_phase0_5.json`、对比 `phase0_5_comparison.json`。Phase 0 旧原始记录和脚本未改。

命令（工作树根目录，均 exit 0）：

```powershell
# 全量：临时审计脚本，不修改测试；仅排除真实 DeepSeek 的两个测试
.\.venv\Scripts\python.exe -X utf8 C:\Users\h000_\AppData\Local\Temp\m3-phase0-5-offline.py C:\Users\h000_\Documents\ChatGPT\agent项目改进\knowledge-agent-m3-phase0
# 独立 focused 回归 67/67、历史安全回归 3/3；最终全量也覆盖这些
..\knowledge-agent-m2\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_m3_answer_policy tests.test_m3_decision_policy tests.test_m3_scripted_equivalence tests.test_aftersales_service tests.test_aftersales_golden
..\knowledge-agent-m2\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_m3_history_safety
# 原始 c/d 结果保留；修正隔离后的完整复现请用新文件名，避免覆盖本轮证据
.\.venv\Scripts\python.exe -X utf8 -m eval_m3.spike.run_deepseek_phase0_5 --dotenv ..\knowledge-agent\.env --repeats 3 --output deepseek_phase0_5_replay.json
# 本轮修正隔离后实际补跑的命令
.\.venv\Scripts\python.exe -X utf8 -m eval_m3.spike.run_deepseek_phase0_5 --dotenv ..\knowledge-agent\.env --repeats 3 --dialogues f --output deepseek_phase0_5_progress.json
```

### 需要用户决定

1. **建议 Phase 3 加入可信的产品 pending/审批状态来源，并把进度问题绑定到当前申请对象。**
   否则只靠历史和 `get_after_sales_case(order_id)` 会稳定错接到同订单的旧售后单。需要决定是否纳入
   Phase 3；本轮没有擅自扩展工具、状态证据或确定性回复能力。
2. **建议将 c2 R3 的“事实正确但没有本轮依据”作为 KB-DEV 失败条件。** 现有 ref 校验发现不了这种
   语义复用；继续改数据消息/重查策略可在后续阶段做，运行时一致性关卡仍按第 6 版放在 Deferred。
3. c1 的一次 answer_unavailable 与 d 的换货干扰保留为质量问题，后续基本功能调优时需要关注；
   本轮未提高生成 token 上限、未改冻结回答 prompt。Ollama 用例修复建议可另开测试隔离任务。

本轮只在 `m3-phase0` 提交，没有推送或合并。为抽查临时启动的 Ollama 在结束后关闭；
未改变其他工作树、冻结目录或真实业务数据库。
