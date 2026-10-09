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
