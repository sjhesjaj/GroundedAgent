# 交接文档：Stage 0（LLMProvider）· Stage 1（Agent Trace）· Stage 2（Diagnostic Eval）· Stage 2.1（Eval Environment）· Stage 2.5（Qwen vs DeepSeek）· Integration Milestone（合入 main M10）· Stage 3（Freshness planning）

> 本文件按阶段累积。**Stage 3 — Freshness planning 见 §15**（A′ `307a12b`，validation `5f0ff42`，h008 已修复）；**Integration Milestone — 合入 origin/main@bac4d69 见 §14**（merge `f784f3e`，结果 commit `9f16680`，post-main-integration baseline）；**Stage 2.5 — Qwen vs DeepSeek 见 §13**（结果 commit `75cce92`）；**Stage 2.1 — Eval Environment 见 §12**（环境 `eval-env-v1`，baseline commit `ffdac42`）；**Stage 2 — Diagnostic Eval 见 §11**（实现 commit `3f1ff97`）；**Stage 1 — Agent Trace 见 §10**（实现 commit `8899d66`）。§0–§9 是 Stage 0 和它的 housekeeping 部分，保留当时的原文。§0–§9 里说"没有进入 Trace 阶段"，指的是 Stage 0 结束时的状态。

# Stage 0 交接：统一 LLMProvider（本地 Ollama/Qwen + DeepSeek API）

- 日期：2026-09-23
- 分支：`stage0-llm-provider`（本地分支，**未 push**）
- 基线 tag：`stage0-baseline` → `db3653ab484fde183e2ecf08cd1cf76480458588`（干净的 `main`）
- 实现 commit：`416fd0d890243dd321d3e301fc67f83ec63e3395`
- 本文件单独放在一个 docs commit 里（在实现 commit 之后）
- OpenViking POC 的未提交改动已保存到本地分支 `wip/openviking-poc@6adfe31`（未 push），Stage 0 没有包含其中任何内容
- 按要求在此停止，**没有进入 Trace 阶段**

## 0. 验收标准逐条结论

| # | 验收标准 | 结论 | 证据 |
|---|---|---|---|
| 1 | 基线冻结：tag、Qwen Eval ≥2 次、记录环境 | ✅ 完成（3 次） | `stage0-baseline` tag，`eval/baseline_qwen.json` |
| 2 | 统一 Provider 返回文本、token、耗时；业务层最小改动 | ✅ 完成 | `llm_provider.LLMResponse`；业务层 `git diff -w` 为 +37 / −113 行，公开函数签名不变 |
| 3 | 模型差异在 Provider 层处理 | ✅ 完成 | Qwen 的 `<think>` 和 `message.thinking`、DeepSeek 的 `reasoning_content` 都进入 `reasoning` 字段，业务层只拿到 `content` |
| 4 | 模型通过配置切换，DeepSeek 模型 ID 查官方文档 | ✅ 完成 | `.env.example`；模型 ID 于 2026-09-23 从官方文档核实（见 §2.4） |
| 5 | Qwen 重跑 Eval 与基线一致 | ✅ 完成 | 3 次都是 39/40，逐题 0 翻转，prompt 和请求体完全相同（§4） |
| 6 | DeepSeek 通过同一接口跑通至少 1 条真实 Query | ✅ 完成 | `eval/deepseek_smoke.json`（§6） |
| 7 | Key 只从 .env 读；有 .env.example；.gitignore 含 .env；git 历史无 Key | ✅ 完成，但有风险（见 §7 R1） | §5.3 扫描结果 |
| 8 | 单元测试 mock、不联网；真实调用测试没有 Key 时跳过 | ✅ 完成 | 33 个 mock 测试；2 个真实调用测试没有 Key 时 `skipIf` 跳过 |
| 9 | 一个清晰的 commit | ✅ 完成 | 实现只有一个 commit `416fd0d`，HANDOFF 另外一个 docs commit |

基线里本来就有 1 个失败的测试，不是 Stage 0 引入的。它已在后续的 housekeeping 中通过测试隔离修复（§8.2）；housekeeping 之后，全量 665 个测试全部通过。

## 1. 修改的文件

**业务代码（最小改动）**

| 文件 | 改动 |
|---|---|
| `rag.py` | 7 处手写的 `requests.post(.../api/chat)` 换成 `llm_provider.get_provider().chat()` / `.chat_stream()`；调用点里剥 think 的代码删掉，改由 Provider 处理；`CHAT_MODEL` 改为从配置读取。Prompt、检索参数、Chunk 结构、公开函数签名都没动。`answer_stream` 只把 NDJSON 读取换成了 `chat_stream()`，提取 JSON 的状态机原样保留。diff 行数看起来多，是因为函数主体整体少了一级缩进 |
| `agent.py` | `decide_action`（工具调用）和 `summarize_knowledge_base` 两处调用改走 Provider；`clean_content()` 保留 |
| `.gitignore` | 加一行 `.env` |
| `tests/__init__.py` | 测试启动时关闭 `.env` 加载，并固定用 Ollama provider，让单元测试不受开发者本地配置影响 |

**新增**

| 文件 | 用途 |
|---|---|
| `llm_provider.py` | `LLMProvider` 接口、`OllamaProvider`、`OpenAICompatibleProvider`（DeepSeek）、配置加载、缓存和 `reset_provider()` |
| `.env.example` | 配置模板，不含任何 Key |
| `tests/test_llm_provider.py` | 33 个单元测试，全部 mock |
| `tests/test_llm_provider_live.py` | 2 个真实 DeepSeek 调用测试，`.env` 里没有完整 DeepSeek 配置时自动跳过 |
| `eval/run_stage0_eval.py` | 包一层 `evaluate_answerability.py`（被包的脚本本身没改），补记环境元数据、被动监听每个 `/api/chat` 请求体、整理出逐题结果 |
| `eval/compare_stage0.py` | 基线和回归的对比；判定标准在拿到回归数据之前就写死了 |
| `eval/deepseek_smoke.py` | 跑一条真实 Query，记录逻辑 prompt 和实际发出的 prompt 的差异 |
| `eval/baseline_qwen.json`、`eval/regression_qwen.json` | 基线和回归结果：环境、配置、prompt 哈希、聚合指标、每题 × 3 次的明细 |
| `eval/stage0_comparison.json` | 对比结论 |
| `eval/deepseek_smoke.json` | 冒烟测试的完整记录（不含请求头，也就不含 Key） |
| `eval/*.console.txt`、`eval/runs/**` | 评测器的原始输出，作为留档 |

**没有改的**：`api.py`、`chat_orchestration.py`、`orchestration/*`、`evaluate_*.py`、`wiki_maintenance/*`、所有 Prompt、所有检索和 RAG 参数。

## 2. 关键设计说明

### 2.1 接口

```python
@dataclass(frozen=True)
class LLMResponse:
    content: str                 # 最终文本，已去掉思考内容
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_seconds: float
    provider: str; model: str
    reasoning: str | None        # 思考内容，只供调试，业务层不用
    tool_calls: tuple[ToolCall, ...]   # ToolCall(name, arguments: dict)
    finish_reason: str | None
    prompt_adaptations: tuple[dict, ...]  # 实际 prompt 与逻辑 prompt 的每一处差异

provider.chat(messages, *, response_format=None | "json" | schema_dict,
              tools=None, temperature=None, max_tokens=None) -> LLMResponse
provider.chat_stream(messages, *, response_format=None, temperature=None,
                     max_tokens=None) -> LLMStream   # 迭代得到文本增量；迭代结束后 .response 带用量和耗时
llm_provider.get_provider()    # 按配置构造，进程内缓存
llm_provider.reset_provider()  # 清掉缓存；测试切换环境变量后要调用
```

### 2.2 为什么这样设计

1. **`OllamaProvider` 发出的请求体和原来逐字段一致**，连字段顺序都一样：`think:false`、`format`、`options.temperature`、`tools`、超时（普通请求 180 秒；流式是连接 10 秒 + 读取 180 秒）。"Qwen 无回归"最强的证据不是指标对得上，而是**发出去的请求本来就没变**。回归时的请求监听证实了这一点（§4.3）。
2. **仍然直接调用 `requests.post`，不引入 openai SDK。** 现有测试是 patch 全局的 `requests.post`，还按顺序数调用次数，这样做才能不改一个旧测试就全部通过。DeepSeek 用的是 OpenAI 兼容的 HTTP 接口，用 `requests` 就够了，不增加依赖。
3. **网络和 HTTP 异常不包装，原样抛给调用方。** `rerank` 等函数里 `except requests.RequestException` 这类降级逻辑因此保持不变。DeepSeek 的 HTTPError 会带上响应体里的错误原因，但绝不带请求头，所以不会带出 Key。
4. **模型差异只在 Provider 层处理**：
   - Qwen3（本机实际是 Qwen3-4B-Thinking-2507）：`content` 里的 `<think>` 按原 `answer()` 的规则剥离（有 `</think>` 就取它后面的部分；否则删掉完整的 think 块和没闭合的尾部 think 块），`message.thinking` 字段的内容放进 `reasoning`。
   - DeepSeek：思考模式默认是**开启**的，Stage 0 固定发送 `thinking: {"type": "disabled"}`，与 Qwen 的 `think:false` 语义对齐；`reasoning_content` 只放进 `reasoning`。
   - 用量：Ollama 取 `prompt_eval_count` / `eval_count`，DeepSeek 取 `usage.prompt_tokens` / `usage.completion_tokens`。
5. **DeepSeek 的 JSON 模式适配（按你的决定执行）**：DeepSeek 只支持 `{"type": "json_object"}`，并要求 prompt 里出现 "json"。
   - 传入 schema 时：改用 `json_object`，并在第一条 system 消息末尾追加一段根据 schema 生成的格式说明（没有 system 消息就新建一条）。
   - 传入 `"json"` 且 prompt 里已经有 "json" 时（比如 rerank 的 prompt）：不改。
   - 每一处改动都记在 `prompt_adaptations`，冒烟记录里也同时保存了逻辑 messages 和实际 messages。
   - 调用方传入的 messages 不会被改动（有单元测试覆盖）。Qwen 路径上没有任何适配。
6. **配置按 provider 分前缀**：`LLM_PROVIDER` 选择后端，`OLLAMA_*` / `DEEPSEEK_*` 各自配置自己的参数。这样切换只需要改一个变量，真实调用测试也可以不受 `LLM_PROVIDER` 影响、单独读 DeepSeek 的配置。
   - Ollama 在代码里保留了原来的默认值（`localhost:11434` / `qwen3:4b`），没有 `.env` 时行为和原来一样。
   - **DeepSeek 在代码里没有任何默认值**，缺 base_url、model 或 key 就直接报错，错误信息里会列出缺哪些。
   - 读取顺序是进程环境变量优先，然后是 `.env`。**唯一的例外是 API Key：只从 `.env` 读**，严格按验收标准 7 字面执行，环境变量 `DEEPSEEK_API_KEY` 会被忽略。如果以后要接 CI，可以放宽这一条。
7. **缓存和清理**：`get_provider()` 在进程内缓存一个实例；`reset_provider()` 清掉缓存。`tests/__init__.py` 设置了 `LLM_DOTENV=""` 并固定 `LLM_PROVIDER=ollama`，这样开发者本地 `.env` 里的 `LLM_PROVIDER=deepseek` 也不会让现有测试去访问 DeepSeek。
8. **Embedding 不走 LLMProvider**：DeepSeek 没有 embedding 接口，所以 `rag.OLLAMA_URL` 这个常量保留，继续给 embedding 和健康检查用。

### 2.3 有意保留的边界行为变化

下面几种情况只有在极端输出下才会出现，本次 Eval 里一次都没有触发（逐题 0 翻转，请求形态完全相同）：

1. `answer_structured` 原来不剥 think 就直接 `json.loads`。如果 content 里混进了 `</think>`，原来会解析失败并退回 `answer()`；现在 Provider 已经先剥掉了，会直接解析成功。这正是验收标准 3 要的效果。
2. `rerank` / `select_for_subquestions` 原来只处理带 `</think>` 的情况，现在也会删掉没闭合的 `<think>…`。
3. Ollama 返回的 message 里如果完全没有 `content` 字段：原来是 `KeyError`，现在当作空字符串，后面的 JSON 解析失败后走原来的降级分支。

### 2.4 DeepSeek 官方信息（2026-09-23 在 api-docs.deepseek.com 查证）

- base_url：`https://api.deepseek.com`（OpenAI 格式）。
- 模型：`deepseek-flash`（DeepSeek-V4.1-Flash）和 `deepseek-v4-pro`（DeepSeek-V4-Pro-0813）。旧名 `deepseek-v4-flash` 仍然能用，但对应的模型已下线。本次使用 `deepseek-flash`。
- 思考模式默认开启，用 `{"thinking": {"type": "enabled/disabled"}}` 切换；开启时 `temperature` 不生效，思考内容在 `reasoning_content` 字段。
- JSON Output：`response_format: {"type": "json_object"}`，prompt 里必须出现 "json"；官方说明偶尔会返回空 content。

## 3. 测试命令和输出原文

```powershell
.\.venv\Scripts\python.exe -m py_compile agent.py api.py app.py rag.py storage.py llm_provider.py
```
```
py_compile exit=0
```

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover
```
这是 `.env` 已配置 DeepSeek Key 时的结果，真实调用测试也跑了：
```
======================================================================
FAIL: test_missing_wiki_is_a_fixed_answer (tests.test_orchestrated_chat.FixedAnswerTests.test_missing_wiki_is_a_fixed_answer)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\Users\h000_\Documents\ChatGPT\agent项目改进\knowledge-agent\tests\test_orchestrated_chat.py", line 218, in test_missing_wiki_is_a_fixed_answer
    self.assertEqual(body["answer"], MESSAGE_NO_WIKI)
AssertionError: '年假为 5 天。[来源 1]' != '当前没有可用的 Wiki 页面。'
- 年假为 5 天。[来源 1]
+ 当前没有可用的 Wiki 页面。


----------------------------------------------------------------------
Ran 665 tests in 18.925s

FAILED (failures=1)
```
- 在 `stage0-baseline`（改动前）上跑同一条命令：`Ran 630 tests ... FAILED (failures=1)`，失败的是**同一个测试**。
- 在 Stage 0 代码上、没有 `.env` 时：`Ran 665 tests ... FAILED (failures=1, skipped=2)`。

所以 Stage 0 新增 35 个测试，没有引入任何新的失败。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_llm_provider tests.test_llm_provider_live -v
```
（以下输出截掉了前面 33 行 `... ok`）
```
test_plain_chat_returns_text_and_usage (tests.test_llm_provider_live.DeepSeekLiveTests.test_plain_chat_returns_text_and_usage) ... ok
test_schema_request_returns_parseable_json (tests.test_llm_provider_live.DeepSeekLiveTests.test_schema_request_returns_parseable_json) ... ok

----------------------------------------------------------------------
Ran 2 tests in 1.746s

OK
```
没有 Key 时同一条命令的结果：`Ran 35 tests in 0.026s  OK (skipped=2)`，跳过原因是 `DeepSeek not configured in .env: ... 缺少配置：DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, DEEPSEEK_API_KEY`。

Eval：
```powershell
.\.venv\Scripts\python.exe -X utf8 eval\run_stage0_eval.py --label baseline_qwen --runs 3     # 在 stage0-baseline 上跑
.\.venv\Scripts\python.exe -X utf8 eval\run_stage0_eval.py --label regression_qwen --runs 3   # 在 Stage 0 代码上跑
.\.venv\Scripts\python.exe -X utf8 eval\compare_stage0.py eval\baseline_qwen.json eval\regression_qwen.json
.\.venv\Scripts\python.exe -X utf8 eval\deepseek_smoke.py
```

## 4. 基线 Eval 与回归 Eval 对比

### 4.1 环境（两次运行相同）

| 项 | 值 |
|---|---|
| 数据集 | `eval_answerability_validation_v1.json`，40 题，sha256 见 `eval/*.json` 的 `dataset_sha256` 字段；`blind_v2` 没有碰 |
| 基线代码 | `db3653a`（`stage0-baseline`），已跟踪文件没有改动 |
| 回归代码 | 当时还没提交的 Stage 0 代码。`regression_qwen.json` 里的 `code_sha256` 已核对，和 `416fd0d` 中的 `rag.py`、`agent.py`、`llm_provider.py`、`chat_orchestration.py`、`evaluate_answerability.py` 完全一致 |
| Chat 模型 | `qwen3:4b`，digest `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7`，权重 blob `sha256-3e4cb141…4e4f`（和 registry 上当前的 `qwen3:4b` 一致），Qwen3-4B-Thinking，**Q4_K_M**，4.0B |
| Embedding | `nomic-embed-text`，F16，137M |
| Ollama | 0.32.15 |
| Wiki | `data/wiki` 已发布的 `build-0001`，`current.json` sha256 `e349bd55…4f61`（这个目录被 gitignore，属于环境输入） |
| Prompt | 4 个 system prompt，sha256 前缀分别是 `38999f71`（rerank）、`519efb55`（子问题选择）、`b20d9a0b`（证据复查）、`f3c643ca`（回答）；全文存在 `observed_llm_requests.system_prompts` |
| 检索配置 | chunk 220/40；BM25 k1=1.5、b=0.75；RRF k=60、向量权重 1.0、关键词权重 1.2；top_k=4、candidate_k=8；BM25 快速路径阈值 ≥3.0 且 ≥1.5× 第二名 |
| Python / OS | 3.12.14 / Windows-11-10.0.26200 |

### 4.2 判定标准（在回归运行之前写进 `eval/compare_stage0.py`）

1. **聚合指标**：回归每一次的每个指标，都落在基线 3 次的 [最小值, 最大值] 区间内，允许上下各多 1 题（按该指标的分母折算）。
2. **逐题**：
   - 基线 3/3 通过、回归 ≤1/3 通过，算**硬回归 → 失败**。
   - 基线 3 次行为完全一致、回归里一次都没出现这个行为，算**行为翻转 → 失败**。
   - 其他通过次数的变化只报告，不判失败。
3. **请求一致性**：回归发出的每个 system prompt、每种请求形态都必须在基线里出现过，出现新的就算失败。

### 4.3 结果（`eval/compare_stage0.py` 输出原文）

```
## Aggregate (per run)
| metric | baseline runs | regression runs | allowed | ok |
|---|---|---|---|---|
| passed_cases | 39 / 39 / 39 | 39 / 39 / 39 | 38 – 40 | ✅ |
| pass_rate | 97.5% / 97.5% / 97.5% | 97.5% / 97.5% / 97.5% | 95.0% – 100.0% | ✅ |
| answer_success_rate | 95.0% / 95.0% / 95.0% | 95.0% / 95.0% / 95.0% | 90.0% – 100.0% | ✅ |
| false_refusal_rate | 5.0% / 5.0% / 5.0% | 5.0% / 5.0% / 5.0% | 0.0% – 10.0% | ✅ |
| unanswerable_refusal_rate | 100.0% / 100.0% / 100.0% | 100.0% / 100.0% / 100.0% | 91.7% – 100.0% | ✅ |
| refusal_mechanism_match | 100.0% / 100.0% / 100.0% | 100.0% / 100.0% / 100.0% | 91.7% – 100.0% | ✅ |
| boundary_accuracy | 100.0% / 100.0% / 100.0% | 100.0% / 100.0% / 100.0% | 87.5% – 100.0% | ✅ |
| citation_presence_rate | 100.0% / 100.0% / 100.0% | 100.0% / 100.0% / 100.0% | 95.0% – 100.0% | ✅ |
| citation_index_validity | 100.0% / 100.0% / 100.0% | 100.0% / 100.0% / 100.0% | 95.0% – 100.0% | ✅ |
| required_source_coverage | 100.0% / 100.0% / 100.0% | 100.0% / 100.0% / 100.0% | 95.0% – 100.0% | ✅ |
| expected_fact_hit_rate | 95.0% / 95.0% / 95.0% | 95.0% / 95.0% / 95.0% | 90.0% – 100.0% | ✅ |
| expected_fact_group_hit_rate | 91.7% / 91.7% / 91.7% | 91.7% / 91.7% / 91.7% | 86.7% – 96.7% | ✅ |
| route_accuracy | 100.0% / 100.0% / 100.0% | 100.0% / 100.0% / 100.0% | 97.5% – 100.0% | ✅ |

## Per-case flips
- hard_regression: 0
- behaviour_flip: 0
- hard_improvement: 0
- soft_flip: 0

## Request identity
- system prompts identical: True (no new: True)
- request shapes identical: True (no new: True)
- /api/chat calls baseline vs regression: 133 vs 133
    - format=json options=None tools=False prompt=38999f71e2bf: 24 vs 24
    - format=json options=None tools=False prompt=519efb557691: 3 vs 3
    - format=schema:75d4773336812270 options={'temperature': 0} tools=False prompt=b20d9a0b473b: 24 vs 24
    - format=schema:75d4773336812270 options={'temperature': 0} tools=False prompt=f3c643ca9fc4: 82 vs 82

VERDICT: NO REGRESSION (aggregate=True, flips=True, requests=True)
```

其他指标：

| | 基线 | 回归 |
|---|---|---|
| 跨次稳定性 | 100%（39 题 3/3 通过，1 题 0/3） | 100%（同样的 39 + 1） |
| 3 次都失败的题 | `answer_document_h008` | `answer_document_h008` |
| 耗时 p50 / p95 / max | 2.83 / 10.52 / 10.94 秒 | 2.83 / 10.29 / 10.51 秒 |
| 评测器自带的 7 项门禁 | 全部通过 | 全部通过 |

`answer_document_h008` 3 次都判成 `policy_refuse`，这是 evidence policy 的判定，**没有调用模型**，所以和 Provider 无关。按 scope 要求不处理 Badcase。

## 5. git 信息

### 5.1 实现 commit 的 `git diff --stat stage0-baseline 416fd0d`

```
 .env.example                           |   25 +
 .gitignore                             |    1 +
 agent.py                               |   52 +-
 eval/baseline_qwen.console.txt         |  701 ++++++++++
 eval/baseline_qwen.json                | 2260 +++++++++++++++++++++++++++++++
 eval/compare_stage0.py                 |  175 +++
 eval/deepseek_smoke.console.txt        |   11 +
 eval/deepseek_smoke.json               |  133 ++
 eval/deepseek_smoke.py                 |  122 ++
 eval/regression_qwen.console.txt       |  701 ++++++++++
 eval/regression_qwen.json              | 2278 ++++++++++++++++++++++++++++++++
 eval/run_stage0_eval.py                |  277 ++++
 eval/runs/baseline_qwen/run-1.json     | 2072 +++++++++++++++++++++++++++++
 eval/runs/baseline_qwen/run-2.json     | 2072 +++++++++++++++++++++++++++++
 eval/runs/baseline_qwen/run-3.json     | 2072 +++++++++++++++++++++++++++++
 eval/runs/baseline_qwen/summary.json   |  159 +++
 eval/runs/baseline_qwen/summary.md     |   65 +
 eval/runs/regression_qwen/run-1.json   | 2072 +++++++++++++++++++++++++++++
 eval/runs/regression_qwen/run-2.json   | 2072 +++++++++++++++++++++++++++++
 eval/runs/regression_qwen/run-3.json   | 2072 +++++++++++++++++++++++++++++
 eval/runs/regression_qwen/summary.json |  159 +++
 eval/runs/regression_qwen/summary.md   |   65 +
 eval/stage0_comparison.json            |  318 +++++
 llm_provider.py                        |  528 ++++++++
 rag.py                                 |  229 ++--
 tests/__init__.py                      |    8 +
 tests/test_llm_provider.py             |  404 ++++++
 tests/test_llm_provider_live.py        |   44 +
 28 files changed, 20964 insertions(+), 183 deletions(-)
```

业务层忽略空白后的真实改动量（`git diff -w --stat`）：

```
 .gitignore        |  1 +
 agent.py          | 46 +++++++++------------------
 rag.py            | 95 ++++++++-----------------------------------------------
 tests/__init__.py |  8 +++++
 4 files changed, 37 insertions(+), 113 deletions(-)
```

新增的 2 万多行里，绝大部分是 Eval 留档（`eval/runs/**` 和 JSON 结果），代码只占少数。

### 5.2 分支和提交

- `stage0-llm-provider`：`db3653a` → `416fd0d`（实现）→ docs commit（本文件）。
- `wip/openviking-poc`：`db3653a` → `6adfe31`，保存了原来未提交的 POC 改动（`rag.py`、`.gitignore` 的修改和 18 个未跟踪文件）。
- 仓库本来没有配置 git 提交身份，所以每次提交都用 `git -c user.name=sjhesjaj -c user.email=184739250+sjhesjaj@users.noreply.github.com` 临时指定（和仓库历史里的作者身份一致），**没有修改全局 git 配置**。
- 没有 push，没有改动任何远程分支。
- 本地 `.git/info/exclude` 里加了一行 `.venv-openviking/`（只在本机生效，不会提交）。原因是 POC 的 `.gitignore` 改动已经移到 wip 分支，`main` 上这个目录会显示为未跟踪。

### 5.3 Key 安全检查

- `git log --all -p | grep -cE 'sk-[A-Za-z0-9]{20,}'` → `0`
- 拿 `.env` 里的 Key 值在 `git log --all -p` 中做精确匹配（没有打印值）→ `0`
- `git log --all --oneline -- .env` → 空，`.env` 从来没有被提交过
- `git check-ignore .env` → 命中 `.gitignore:17:.env`；`.env.example` 没有被忽略，里面的 `DEEPSEEK_API_KEY=` 是空的

## 6. DeepSeek 真实调用的输入和输出

命令：`.\.venv\Scripts\python.exe -X utf8 eval\deepseek_smoke.py`。完整记录在 `eval/deepseek_smoke.json`，里面有逻辑 messages、实际 messages、实际请求参数（不含请求头）和统一格式的响应。

**配置**：`{'provider': 'deepseek', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'timeout': 180.0, 'api_key_set': True}`

**链路**：和 Eval 相同。`chat_orchestration.prepare`（路由 `document_only`，BM25 快速路径，embedding 走本地 Ollama）→ `rag.answer_structured`。整条链路只发生了 1 次 LLM 调用：这条问题走的是 BM25 快速路径，没有触发 rerank；首轮也没有拒答，所以没有触发证据复查。

**输入：逻辑 messages（业务层构造的，没有改动）**

system：
```
你是严谨的企业知识库助手。请直接阅读用户消息中“资料”部分并回答“问题”。只能使用资料里的事实，不得使用外部知识。若资料明确包含答案，必须作答；只有资料确实没有相关信息时才说“根据现有资料无法确定”。最终答案控制在3句话以内，不要展示分析过程、推理步骤或自我检查。在每个关键结论后使用[来源1]这样的编号标注依据。 /no_think
```
user：
```
以下是检索到的资料：

[来源 1：sample_company_rules.md，片段 1]
# 星河科技员工手册（演示资料）
## 请假制度
正式员工入职满一年后，每年享有 5 天带薪年假；工作满三年后增加至 8 天。实习生不享有带薪年假，但每月可申请 1 天事假。请假应提前在系统提交申请，1 天以内由直属主管审批，超过 1 天还需部门负责人审批。

[来源 2：sample_company_rules.md，片段 2]
# 星河科技员工手册（演示资料）
## 薪资发放
公司于每月 10 日发放上一个自然月的工资；遇法定节假日则提前至最近的工作日。工资条通过人力资源系统发送，员工如有疑问应在 5 个工作日内反馈。

[来源 3：sample_company_rules.md，片段 3]
# 星河科技员工手册（演示资料）
## 远程办公
员工每周最多申请 2 天远程办公，须至少提前一个工作日获得直属主管批准。涉及客户现场支持、机房值守的岗位不适用远程办公政策。

[来源 4：sample_company_rules.md，片段 4]
# 星河科技员工手册（演示资料）
## 账号与权限
系统账号仅限本人使用，不得共享密码或验证码。岗位调整时，直属主管应发起权限变更；高权限账号每 90 天复核一次。发现账号异常应立即联系信息安全团队。

请回答问题：员工年假有多少天？
/no_think
```

**实际发出的内容和逻辑 prompt 的差异**：只有一处。`messages[0]`（system）从 167 字变成 312 字，末尾追加了：
```


请只输出一个合法的 JSON 对象，不要输出 JSON 以外的任何内容。该 JSON 必须符合以下 JSON Schema：{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}}}
```
`prompt_adaptations` 里的记录：`[{'kind': 'json_format_instruction', 'reason': 'schema_not_supported_by_json_object', 'position': 'appended_to_messages[0]', ...}]`

实际请求参数：`{'model': 'deepseek-flash', 'stream': False, 'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'}, 'temperature': 0}`

**输出（控制台原文）**
```
Question : 员工年假有多少天？
Provider : deepseek / deepseek-flash @ https://api.deepseek.com
Route    : document_only (ready)
LLM call 1: format=True prompt_tokens=504 completion_tokens=44 latency=1.11s finish=stop adaptations=['schema_not_supported_by_json_object']
  content: {"answer":"正式员工入职满一年后每年享有5天带薪年假，工作满三年后增加至8天[来源1]；实习生不享有带薪年假[来源1]。"}
Answer   : 正式员工入职满一年后每年享有5天带薪年假，工作满三年后增加至8天[来源1]；实习生不享有带薪年假[来源1]。
```
- 统一格式的响应：`content` 是 JSON 字符串，`reasoning=None`（思考已关闭），prompt_tokens=504，completion_tokens=44，latency=1.106 秒，finish_reason=`stop`。
- 业务层 `answer_structured` 解析出来的最终答案就是上面的 `Answer`。
- 端到端耗时 1.53 秒（包含本地检索）。

## 7. 未解决的问题和风险

**R1 · API Key 已暴露（需要你处理）。** 过程中出现过两个 DeepSeek Key：一个贴在了聊天里，另一个一度写在 `.env.example` 里（在提交之前，已原样改名为 `.env`，并用不含 Key 的模板重新生成了 `.env.example`；上面的扫描确认 git 历史里没有它）。两个 Key 都已经出现在对话记录里，**建议都去 DeepSeek 控制台轮换**。

**R2 · ~~`wip/openviking-poc` 分支上的 `.gitignore` 里没有 `.env`。~~ 已在 housekeeping 中解决（§8.1）**：那个分支已补上 `.env` / `.env.*` / `!.env.example`（commit `8463f33`）。

**R3 · POC 以后合并时会和 Stage 0 冲突。** POC 修改过 `rag.answer`、`answer_structured`、`answer_stream`、`build_answer_messages`（加了 `user_memory` 参数），这些正是 Stage 0 改动过的调用点。另外，`scripts/openviking_probe.py` 靠修改 `rag.CHAT_MODEL` 来切换模型、靠 patch `requests.post` 来打开 think；Stage 0 之后前者对 chat 调用已经不起作用（要改用 `OLLAMA_CHAT_MODEL` 配置加 `reset_provider()`）。

**R4 · `wiki_maintenance/ollama_compiler.py` 没有迁移（遗留项，按你的决定）。** Wiki 编译仍然直接请求 Ollama，模型写死为 `qwen3:4b`。所以 `LLM_PROVIDER=deepseek` 时，后台 Wiki 维护**仍然用本地 Qwen**。它已经有 `WikiModel` 注入接口，后续可以写一个适配器接到 Provider 上。

**R5 · ~~基线里本来就失败的测试。~~ 已在 housekeeping 中解决（§8.2）。** `tests.test_orchestrated_chat.FixedAnswerTests.test_missing_wiki_is_a_fixed_answer` 在 `stage0-baseline` 上就失败。原因是测试把 `WIKI_PAGES` 置空了，但本地 gitignored 的 `data/wiki/current.json`（build-0001）被 `wiki_runtime` 优先读取，覆盖了置空的效果。§3 里的测试输出保留的是修复之前的原文。

**R6 · DeepSeek 模式的覆盖面很窄。**
- 真实调用只验证了 `answer_structured` 这一条路径（1 条 Query 加 2 个真实调用测试）。
- `rerank`、`select_for_subquestions`、`decide_action`（工具调用）、`summarize_knowledge_base`、流式 `answer_stream` 走 DeepSeek 的情况**只有 mock 测试，没有真实调用验证**。
- DeepSeek 模式没有跑 Eval，按要求也不要求效果更好。

**R7 · DeepSeek 模式下 prompt 里有只对 Qwen 有意义的内容。**
- Prompt 里的 `/no_think` 会原样发给 DeepSeek。当前没有观察到影响；按"不改 Prompt"的要求保留了。
- 格式说明是 Provider 追加的，所以 DeepSeek 实际收到的 prompt 和 Qwen 不同，差异已经记录在 `prompt_adaptations`。
- 官方文档说 JSON 模式偶尔会返回空 content。遇到这种情况，`answer_structured` 会走原来的降级分支 `answer()`，多一次调用。

**R8 · 思考模式固定关闭。** 如果以后打开思考并同时使用工具调用，DeepSeek 要求在后续请求里回传 `reasoning_content`，否则返回 400。本阶段按要求没有实现这部分。

**R9 · DeepSeek 模式仍然依赖本地 Ollama。** Embedding（`nomic-embed-text`）和 `/api/health` 的健康检查继续走 `rag.OLLAMA_URL` 这个写死的常量；`OLLAMA_BASE_URL` 配置只影响 chat 调用。

**R10 · Ollama 流式输出里的内联 `<think>` 不会被过滤。** 流式模式下只丢弃了单独的 `message.thinking` 字段，没有过滤写在 `content` 里的 `<think>` 标签。目前流式请求都带 JSON Schema，没有观察到这种输出。

**R11 · 真实调用测试会在常规测试运行中访问网络。** 只要 `.env` 里有 Key，`unittest discover` 就会真实调用 DeepSeek 两次（每次约 1 秒，费用很低）。这符合验收标准 8，但如果不希望常规测试联网，可以再加一个显式开关。

**R12 · Eval 的局限。**
- `validation_v1` 是仓库里定位为回归基线的数据集，曾被开发者看过，所以这次的结论只能说明"没有回归"，**不能当作泛化能力的指标**。
- `rerank` 调用没有设置 temperature，本来有随机性。这次 3 次运行的结果完全一致，但并不代表它是确定性的。
- 基线 harness（`eval/run_stage0_eval.py`）是打 tag 之后才写的，没有包含在 tag 的代码树里。它不改任何产品代码，也不改评测逻辑，只做被动记录。

**R13 · 本机 Ollama 的奇怪状态。** `ollama list` 没有列出 `qwen3:4b`，但 `/api/show` 和本地 manifest 都在，Eval 也正常使用了它。精确的 digest 和权重 blob 已经记录在基线里，将来可以核对。

## 8. Stage 0 housekeeping（进入 Stage 1 之前）

本节只做清理，不扩展功能，**没有改动任何生产代码**（`rag.py`、`agent.py`、`llm_provider.py`、`api.py`、`chat_orchestration.py`、`wiki_runtime.py` 都没动），也没有删除或移动任何真实数据。

| 分支 | commit | 内容 |
|---|---|---|
| `wip/openviking-poc` | `8463f338085c21bddbd3109c4b0f7d715de0f896` | 只改 `.gitignore`：忽略 `.env`、`.env.*`，保留 `.env.example` 可提交 |
| `stage0-llm-provider` | `225d228fee12b838dca334e453678b11bf73154e` | 测试隔离、`.gitignore`、eval artifacts 规则 |
| `stage0-llm-provider` | 本文件更新所在的 docs commit | HANDOFF §0 / §7 R2、R5 / §8 / §9 |

### 8.1 .gitignore 规则

两个分支现在都有下面这组规则：

```gitignore
# Local secrets: never commit .env or its variants; the template stays tracked.
.env
.env.*
!.env.example
```

`stage0-llm-provider` 上另外加了：

```gitignore
# Raw eval run output (per-run dumps, console logs). Condensed results stay in eval/.
eval/artifacts/
```

`git check-ignore --no-index` 的核对结果：

| 路径 | 结果 |
|---|---|
| `.env`、`.env.local`、`.env.production` | 忽略 |
| `.env.example` | 不忽略 |
| `eval/artifacts/x/run-1.json`、`eval/artifacts/x.console.txt` | 忽略 |
| `eval/runs/baseline_qwen/run-1.json`、`eval/baseline_qwen.json`、`eval/README.md` | 不忽略 |

Stage 0 已提交的 `eval/runs/` 下 10 个文件仍然被跟踪。

### 8.2 修复 `test_missing_wiki_is_a_fixed_answer`：怎么隔离的

- **根因**：`chat_orchestration.current_wiki_pages()` 优先读取 `wiki_runtime.RUNTIME.published_pages()`，这个模块级单例的根目录是真实的 `data/wiki`。本机上那里有一个已发布的 build-0001，它覆盖了测试里 `patch.object(chat_orchestration, "WIKI_PAGES", ())` 的效果。换一台没有本地 Wiki build 的机器（比如 CI），这个测试就会通过，所以它是一个依赖环境的测试缺陷。
- **修复方式**：在 `OrchestratedChatTests.setUp` 里执行 `patch.object(wiki_runtime, "RUNTIME", wiki_runtime.WikiRuntime(root=<本测试的临时目录>/wiki))`。
  - 这个目录不存在，等同于"还没有发布过 build"，也就是全新 checkout 的状态。
  - `_has_published_build()` 只检查文件是否存在，不会创建目录；临时目录在 `tearDown` 时清理，patch 通过 `addCleanup(patch.stopall)` 恢复。
  - 用的是仓库里已有的隔离写法，和 `test_upload_upsert.py`、`test_cross_document_supersede.py`、`test_wiki_runtime.py` 一样。
- **作用范围**：`OrchestratedChatTests` 和继承它的 7 个测试类（RouteScenario、FixedAnswer、EmptyKnowledgeBase、LegacyCompatibility、Stream、ConcurrencyAndVersion、Isolation）。没有做全局替换，因为 `ImportPurityTests` 要断言全局 `RUNTIME` 指向默认目录。
- **确认影响范围**：写了一个只用于诊断的 runner，把整个测试套件放在空的临时 Wiki 根目录下跑。除了诊断脚本本身预期会触发的 `ImportPurityTests`，唯一受影响的就是这个目标测试。也就是说，全套件只有它依赖本地 `data/wiki`。
- **数据没有被改动**：修复前后 `data/wiki/current.json` 的 sha256 都是 `e349bd55ac1bc534…`，目录下都是 4 个文件。

### 8.3 Eval artifacts 规则

- 写在 `eval/README.md` 里。
- **进 Git**：`eval/<label>.json`（环境、配置、prompt 哈希、每次运行的 summary、aggregate、gates、每个 case 每次运行的结果）、对比结论、冒烟记录、脚本。
- **不进 Git**：`eval/artifacts/`，也就是评测器的原始 `run-N.json` / `summary.*` 和控制台日志。
- `eval/run_stage0_eval.py` 的原始输出目录从 `eval/runs/<label>` 改成了 `eval/artifacts/<label>`，condensed 的 `eval/<label>.json` 不变。用 `--runs 1` 实际跑了一次核对（39/40）：原始文件落在 `eval/artifacts/` 下且被忽略，condensed 文件照常生成。这次核对的产物是一次性的，核对后已删除，没有提交。
- **历史例外**：Stage 0 已提交的 `eval/runs/**` 和 `eval/*.console.txt` 保留在原处，没有移动或改写。

### 8.4 测试结果（在 `225d228` 上，`.env` 已配置 DeepSeek Key）

```
py_compile exit=0
unittest discover exit=0
Ran 665 tests in 16.782s
OK
```
```
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_orchestrated_chat tests.test_llm_provider tests.test_llm_provider_live
Ran 67 tests in 3.407s
OK
```
修复之后单独跑目标测试：`Ran 1 test ... OK`；单独跑 `tests.test_orchestrated_chat`：`Ran 32 tests ... OK`。

### 8.5 diff 摘要（`git diff --stat 34663aa 225d228`）

```
 .gitignore                      |  5 +++++
 eval/README.md                  | 21 +++++++++++++++++++++
 eval/run_stage0_eval.py         |  9 ++++++++-
 tests/test_orchestrated_chat.py |  8 ++++++++
 4 files changed, 42 insertions(+), 1 deletion(-)
```

## 9. Tech Debt

- **TD1 · 拆分 `llm_provider.py`（本阶段按要求不重构）。** 这个文件现在 528 行，把几类职责放在了一起。将来可以拆成：
  - `llm/config.py`：`LLMConfig`、`load_config`、`.env` 解析、`LLMConfigError`
  - `llm/types.py`：`LLMResponse`、`ToolCall`、`LLMStream`、`LLMProvider` Protocol
  - `llm/providers/ollama.py`、`llm/providers/openai_compatible.py`：两个实现，以及 `split_think`、`adapt_json_prompt` 这类只属于某个 provider 的适配逻辑
  - `llm/__init__.py`：`get_provider` / `reset_provider` / `create_provider`，保持现有的导入路径兼容

  拆分时要注意：两个 provider 必须继续在调用时直接使用 `requests.post`，现有测试是 patch 全局 `requests.post` 的。
- **TD2 · Wiki 编译器接入 Provider**（原来的 R4）：给 `WikiModel` 写一个接到 `llm_provider` 的适配器，替代写死的 `OllamaWikiModel`。
- **TD3 · Embedding 与 chat 的配置不统一**（原来的 R9）：`rag.OLLAMA_URL` 仍然是写死的常量，`OLLAMA_BASE_URL` 只影响 chat 调用。
- **TD4 · `run_stage0_eval.py` 只支持 answerability 评测器**，而且名字带着 Stage 0。以后如果要评测别的数据集或 provider，可以把它泛化，并同步更新 `eval/README.md`。
- **TD5 · 真实调用测试没有显式开关**（原来的 R11）：只要 `.env` 里有 Key，常规测试就会联网。

按要求在这里停止，没有进入 Trace 阶段。

---

## 10. Stage 1 — Agent Trace

- 分支：`stage0-llm-provider`（在 Stage 0 之后继续提交，本地，**未 push**）
- 实现 commit：`8899d66184a5017ff713ae4b9b18fc15436a8b65`
- 本节所在的 docs commit 在它之后
- 目标：一个 Agent 请求或 Eval case 失败时，**只看持久化的 Trace**，就能还原它经过了哪些阶段、调用了什么工具、拿到了什么证据、消耗了多少 Token 和时间，以及具体在哪一步出的错
- 按要求在此停止，**没有进入 Stage 2（Eval / Badcase 分类）**

### 10.1 数据模型

两张新表。API 的 Trace 存在 `api.storage.path`（默认 `data/knowledge_agent.db`）；Eval 的 Trace 存在 `eval/artifacts/<label>/traces.sqlite`。表在第一次使用时创建（`IF NOT EXISTS`），`storage.py` 没有改。

**`trace_runs`**：一个请求或一个 Eval case 对应一行。

| 字段 | 含义 |
|---|---|
| `run_id` | uuid4 hex；API 通过响应头 `X-Run-Id` 返回 |
| `schema_version`、`kind`（api/eval）、`entrypoint`、`mode`、`streaming` | 请求形态。流式和非流式共用这张表，用 `streaming` 区分 |
| `session_id`、`client_id`、`question` | 请求输入 |
| `status` | `running` / `completed` / `failed`。**completed 只表示请求正常结束，不代表答案正确** |
| `failed_stage`、`failed_span_id` | failed_stage 取最外层的业务阶段（router/planner/tool_call/evidence/generation/commit；如果异常发生在所有阶段之外，就是 `request`）；具体失败位置看 `failed_span_id` 指向的子 span |
| `error_type`、`error_message`、`error_traceback` | 导致请求中断的异常；已脱敏；消息截断到 1000 字，traceback 截断到 4000 字（保留尾部） |
| `started_at`、`finished_at`、`duration_ms` | 时间 |
| `git_commit`、`git_dirty` | 进程启动后第一次用到时获取，之后缓存 |
| `provider`、`model` | 实际使用的 Provider |
| `prompt_hashes_json` | 本次运行实际用到的 system prompt 的 sha256 列表（只是汇总；逐次调用的信息在 llm_call span 里）。可以和 Stage 0 基线的哈希直接对比 |
| `retriever_config_json` | chunk、BM25、RRF、top_k、candidate_k、快速路径阈值、embed 模型、执行器的 top_k |
| `dataset`、`dataset_sha256`、`case_id`、`eval_run_index` | 只有 Eval 有 |
| `knowledge_version` | API 请求开始时的知识库版本 |
| `llm_calls`、`prompt_tokens`、`completion_tokens`、`llm_latency_ms`、`span_count`、`error_span_count` | 汇总 |
| `trace_overhead_ms` | recorder 自己计时的开销，**只用于内部诊断**（原因见 §10.7） |
| `attributes_json` | 其他事实，例如 `sse_error_event` |

**`trace_spans`**：一个步骤对应一行。字段有 `span_id`、`run_id`、`parent_span_id`、`seq`、`stage`、`name`、`status`（ok/empty/error）、`offset_ms`、`latency_ms`、`input_json`、`output_json`、`error_type`、`error_code`、`error_message`、`error_traceback`，以及只有 llm_call 才有的 `provider`、`model`、`prompt_tokens`、`completion_tokens`，最后是 `attributes_json`。

| stage / name | 记录的内容 |
|---|---|
| `router` / `decide_action`（legacy） | 输入：问题、历史轮数。输出：type、tool、arguments、seconds。走 LLM 分支时下面挂一个 llm_call |
| `planner` / `plan_request` | 输出 `Plan.to_dict()`（route、steps、signals、reason_codes、fallback_used） |
| `planner` / `availability_check` | 输入：steps、chunks 数、wiki 页数、SKU 数。输出：固定答案或 null |
| `tool_call` / `execute_plan` | 执行器整体，下面挂每个工具的子 span |
| `tool_call` / `document_search`、`wiki_query`、`system_query` | name、arguments、result（`ToolResult.to_dict()`，包括 evidence 和检索 trace）、latency（执行器记录的单步耗时）、status、error_code、error_type（执行器捕获的异常类名） |
| `tool_call` / `search_knowledge_base`、`list_knowledge_sources`、`summarize_knowledge_base`（legacy） | arguments 和结果 |
| `evidence` / `evaluate_evidence`（legacy 模式是 `retrieved_sources`） | `PolicyDecision.to_dict()`：outcome、usable_evidence、missing_tools、tool_failures、reason_codes |
| `generation` / `answer_structured`、`answer_stream` | 输入：问题、证据来源、历史轮数。输出：答案 |
| `llm_call` / 调用方函数名（`answer_structured`、`rerank`、`select_for_subquestions`、`decide_action`…） | 逻辑 messages；有适配时还有实际发送的 messages；`logical_prompt_sha256`、`effective_prompt_sha256`、`system_prompt_sha256`、`prompt_adaptations`；content、reasoning、tool_calls、finish_reason；**Stage 0 Provider 给出的 prompt/completion token** 和 latency。流式还有 `delta_count` 和 `first_delta_ms` |
| `commit` / `commit_exchange` | 知识库版本和来源数。知识库版本冲突导致的 409 会在这里记为失败 |

用 `python -m agent_trace --db <库> list [--status failed]` 列出运行记录，用 `show <run_id> [--json]` 把一个 run 还原成文本树。

### 10.2 决策是怎么落实的（对照你给的 11 条约束）

1. **存储位置**：API 的 Trace 跟着 `api.storage.path` 走，测试把 storage 换成临时库，Trace 也就跟着进临时库，所以测试不会污染真实库（已确认：跑完全量测试后，真实库里依然没有 `trace_*` 表）。Eval 的 Trace 写到 `eval/artifacts/<label>/traces.sqlite`，已被 gitignore。
2. **统一的 sanitizer**：key 名匹配 `api_key`、`authorization`、`token`、`password`、`secret`、`client_secret`、`private_key`、`subject_id`、`cookie` 等的字段会被脱敏。`prompt_tokens`、`max_tokens` 这类 token 计数字段不受影响。文本里形如 `sk-…`、`Bearer …` 的值，以及进程已知的 DeepSeek Key 的原值，都会被替换掉。普通业务参数（例如 SKU）保留真实值。**执行器没有改**，所以工具报错时只有它给出的脱敏信息（error_code 和异常类名）。
3. **非工具异常**：记录 error_type、截断后的 message 和 traceback，写库之前统一脱敏。
4. **status 取值**：`running | completed | failed`。工具出错、系统正常给出拒答的请求记为 completed，同时计入 `error_span_count`。
5. **`TRACE_ENABLED`**：默认开启；设为 `0/false/no/off` 时不写任何行、也不返回 header，业务行为不变（有测试覆盖）。
6. **`run_id` 只通过 `X-Run-Id` header 返回**，HTTPException 的响应也带；响应体、SSE 事件和客户端 trace 一个字节都没变（原有的隐私测试照常通过）。
7. **大文本截断**：API 的 Trace 里每个字符串最多 4000 字、每个列表最多 50 项；Eval 的 Trace 完整保留（`truncate=False`）。
8. **failed_stage 取最外层业务阶段**，具体位置看 `failed_span_id`。**失败点按"导致中断的那个异常对象"来匹配，而且这个异常必须一路逃出了该 span 的所有祖先**。被调用方处理掉的错误（例如 rerank 失败后降级）仍然记为 error span，但不会被认定为失败点。这条规则是在一次真实运行中发现问题后改的，见 §10.9 R1。
9. **每个 llm_call** 都记录 logical/effective prompt 的哈希和 prompt adaptation；run 级别的 `prompt_hashes` 只做汇总。
10. **开销以 200 次 mock A/B 为主要指标**，`trace_overhead_ms` 只用于诊断。
11. **没有扩大范围**：没有 Dashboard，没有接 Langfuse/OTel，没有做 Badcase 分类，没有改 Prompt、Retriever 参数和业务决策。

另外两条设计原则：

- **Trace 绝不能让请求失败**：开始记录和写库时的错误都会被记日志后吞掉；recorder 在请求过程中执行的代码（`_trace_bundle`、`_prompt_facts`、`_record_response`）都包在 `agent_trace.safely()` 里。有测试覆盖。
- **流式接口**：Starlette 每次调用 sync generator 的 `next()` 时都会重新复制 context，在生成器里 `set` 的 contextvar 过了第一个 `yield` 就会丢失（我写了一个探测脚本验证过：`step1 v=None`）。所以流式 generator 由 `Run.iterate()` 驱动，每次 `next()` 都在同一个 Context 里执行。客户端提前断开时，run 会被记为 failed，错误类型是 `ClientDisconnected`。

### 10.3 改动的文件

| 文件 | 改动 |
|---|---|
| `agent_trace.py`（新增） | recorder、SQLite 存储、sanitizer、provider 包装层、`Run.iterate`、CLI |
| `api.py` | 4 个入口各开一个 run，记录 router、tool_call、evidence、generation、commit，并设置 `X-Run-Id`；业务逻辑没变（忽略空白后 +131/−24 行，大部分是 `with` 缩进） |
| `chat_orchestration.py` | 在 `prepare()` 里对 planner、availability、execute_plan 做记录，并加了 `_trace_bundle()`。只读，决策不变 |
| `llm_provider.py` | `get_provider()` 在有活动 run 时返回包装过的 provider，没有 run 时返回原对象（+3 行） |
| `eval/run_stage0_eval.py` | 每个 case 开一个 eval run，写入 `trace_run_id`、`trace_db`、`trace_summary`；`code_sha256` 里加上了 `agent_trace.py`。评测器本身没改 |
| `eval/trace_overhead.py`（新增） | TRACE 开/关的 A/B 基准 |
| `eval/README.md` | 补充 Trace 产物的说明，以及如何从失败 case 的 `trace_run_id` 查到 Trace |
| `tests/test_agent_trace.py`（新增） | 28 个测试 |
| `eval/stage1_trace_qwen.json`、`eval/stage1_trace_comparison.json`、`eval/stage1_trace_overhead.json`（新增） | 回归结果、对比结论、开销数据 |

**没有改的**：`orchestration/*`、`rag.py`、`agent.py`、`storage.py`、`evaluate_answerability.py`、`wiki_*`、前端、所有 Prompt、所有检索参数。

### 10.4 新增测试（`tests/test_agent_trace.py`，28 个，全部 mock，不联网）

所有断言都通过**新开的 SQLite 连接**读取，不看内存里的对象。

| 场景 | 测试 |
|---|---|
| 普通成功请求 | `test_plain_request_replays_every_stage_from_sqlite`：阶段顺序、元数据、tool 参数和结果、evidence、llm_call 的 token 和哈希、`X-Run-Id` 与库中记录一致、响应体的 key 不变 |
| 多步 Tool Calling | `test_multi_step_tool_calls_are_recorded_in_order`（先 document_search 再 system_query，SKU 保留真实值，subject_id 为 null）、`test_wiki_and_document_route_records_both_tools` |
| Tool 失败 | `test_tool_exception_is_an_error_span_in_a_completed_run`（run 为 completed，error span 数为 1，evidence 显示 refuse + tool_error）、`test_executor_programmer_error_fails_the_run_at_tool_call`（failed，failed_stage=tool_call，有 traceback） |
| 生成阶段失败 | `test_model_failure_fails_the_run_at_generation`（Bearer token 已脱敏）、`test_error_handled_inside_a_tool_is_not_blamed_for_a_later_failure`（复现 §10.9 R1 的真实场景）、`test_commit_conflict_fails_at_commit` |
| Streaming | `test_streaming_run_uses_the_same_model`、`test_stream_failure_mid_generation_is_finalized`、`test_legacy_stream_is_traced` |
| legacy 模式 | 规则路由、LLM 路由（router 下挂 llm_call）、锁冲突返回的 409 响应也带 `X-Run-Id` |
| 开关和健壮性 | `TRACE_ENABLED=0` 时不写任何行、业务不变；写库失败时请求照常完成；recorder 自身出 bug 时请求照常完成；没有活动 run 时 provider 不被包装；CLI 能还原 |
| recorder 单元测试 | sanitizer（3 个）、截断（API 截断、Eval 完整）、finish 幂等以及外层 stage 的判定、按异常对象身份匹配失败点、跨线程池的流式 generator、流提前关闭记为 failed、关闭开关时返回 null run |

### 10.5 全量测试结果（在 `8899d66` 的代码上）

```
.\.venv\Scripts\python.exe -m py_compile agent.py api.py app.py rag.py storage.py llm_provider.py agent_trace.py chat_orchestration.py
py_compile exit=0
.\.venv\Scripts\python.exe -X utf8 -m unittest discover
Ran 693 tests in 24.126s
OK
```

665 个原有测试 + 28 个新测试。`.env` 里配有 DeepSeek Key，所以 2 个真实调用测试也一起跑了。跑完之后真实的 `data/knowledge_agent.db` 里仍然只有 `knowledge_chunks`、`conversations`、`messages`、`sqlite_sequence`、`app_meta` 这几张表。

### 10.6 validation_v1 回归（带 Trace，和 Stage 0 的 `regression_qwen.json` 对比）

```
.\.venv\Scripts\python.exe -X utf8 eval\run_stage0_eval.py --label stage1_trace_qwen --runs 3
.\.venv\Scripts\python.exe -X utf8 eval\compare_stage0.py eval\regression_qwen.json eval\stage1_trace_qwen.json
```

- **VERDICT: NO REGRESSION**：3 次运行都是 39/40，12 项指标每一次都落在允许区间内；逐题：硬回归 0、行为翻转 0、硬改善 0、软翻转 0；system prompt 和请求形态完全一致，没有出现新的。完整结果在 `eval/stage1_trace_comparison.json`。
- `/api/chat` 调用次数是 133 对 135，多出来的 2 次都是 rerank（24 → 26）。**原因是只用 Trace 查出来的**：`answer_multi_h001` 在第 1、2 次运行时，`select_for_subquestions` 让模型返回了不在候选集里的 ID（`["6","3"]`）或空列表，触发了已有的降级逻辑，多调一次 rerank。这个调用本来就没设 temperature，属于模型输出的随机波动，和 instrumentation 无关；这个 case 的判定结果也没变。
- Trace 覆盖情况：120 次 case 运行都有各自不同的 `trace_run_id`，Trace 库里 120 个 run 全部是 completed，共 755 个 span，库文件 1.5MB（Eval 不截断，平均每个 run 约 12.5KB）。
- `code_sha256`（rag、agent、llm_provider、chat_orchestration、evaluate_answerability、agent_trace）和 `8899d66` 中的文件逐一一致。
- 端到端耗时仅供参考，因为 LLM 本身的波动会淹没 Trace 的开销：p50 / p95 / max 在 Stage 0 是 2.83 / 10.29 / 10.51 秒，Stage 1 是 2.80 / 10.37 / 11.72 秒。

### 10.7 Instrumentation 开销（主要指标：TRACE 开/关 A/B，每组 200 次）

`eval/trace_overhead.py`：真实的请求路径（FastAPI → planner → executor → evidence → answer_structured/answer_stream → provider → SQLite commit），只把检索和模型换成瞬间返回的 mock；两组请求逐个交替执行，每组先预热 20 次。

| 场景 | 关闭 平均 / p50 / p95 | 开启 平均 / p50 / p95 | 平均差值（95% CI） | p50 差值 | p95 差值 |
|---|---|---|---|---|---|
| orchestrated `/api/chat` | 26.03 / 22.79 / 44.60 ms | 45.38 / 43.38 / 60.05 ms | **+19.35 ms** [+17.97, +20.74] | +20.59 | +15.45 |
| orchestrated `/api/chat/stream` | 27.27 / 24.43 / 48.41 ms | 46.85 / 44.54 / 63.75 ms | **+19.58 ms** [+18.07, +21.08] | +20.12 | +15.33 |

- **怎么理解**：每个请求固定多出约 20ms。在 mock 请求上，这相当于 +74%；在真实请求上（validation 的 p50 是 2.8 秒），大约是 **+0.7%**。
- **时间花在哪里**（用 cProfile 看的）：几乎全部花在每个请求多出来的 2 次 SQLite 事务上（启动时插入 run，结束时写入 span 并更新 run），每次 connect、commit、close 合起来约 5ms（Windows 上 WAL 在最后一个连接关闭时会做 checkpoint）。Python 这边的 span、sanitize 和哈希每个请求不到 1ms。
- recorder 自计时的 `trace_overhead_ms`（p50 9.0 / p95 13.1 ms）比 A/B 测出来的差值小，因为 Trace 写入会让 WAL 变大，拖慢随后会话存储关闭连接时的 checkpoint，而这部分时间算在了存储层头上。**所以它只能作为诊断，不能当开销指标。**
- 按要求只做测量，不做优化；优化方向记在 §10.10。

### 10.8 Trace 样例（真实请求：真实 API、真实检索、真实 Qwen，用的是临时库）

**成功的 run**（document_system 路线，两个工具）：

```
run 1f8305f785ad4b59811cfdcaf248172c  completed
  api /api/chat mode=orchestrated streaming=False  2026-09-23T10:43:07.912+00:00  4752.8ms
  commit=5baf106ace40+dirty  provider=ollama/qwen3:4b  llm_calls=1 tokens=489+109
  question: 请假制度原文怎么写的，另外 SKU-A100 还有多少库存？
    · [planner] plan_request ok 0.1ms
    · [planner] availability_check ok 0.0ms
    · [tool_call] execute_plan ok 392.6ms
      · [tool_call] document_search ok 391.9ms
      · [tool_call] system_query ok 0.1ms
    · [evidence] evaluate_evidence ok 0.1ms
    · [generation] answer_structured ok 4144.8ms
      · [llm_call] answer_structured ok 4144.7ms tokens=489+109
    · [commit] commit_exchange ok 12.4ms
```

答案：`…请假应提前在系统提交申请…[来源 1] SKU-A100 当前库存为 42 件。[来源 5]`。这些样例是在代码提交之前生成的，所以 commit 显示为 `5baf106+dirty`。

**失败的 run**：chat provider 指向一个不存在的端口，embedding 仍然走真实的 Ollama，于是出现真实的 `ConnectionError`。

```
run 5a51d396f01e4255a3852403c083f5dc  failed  failed_stage=generation
  api /api/chat mode=orchestrated streaming=False  2026-09-23T10:43:12.569+00:00  6201.5ms
  commit=5baf106ace40+dirty  provider=ollama/qwen3:4b  llm_calls=2 tokens=0+0
  question: 年假最多可以休多少天
  error: ConnectionError: HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded with url: /api/chat (Caused by NewConnectionError(...[WinError 10061]...))
    · [planner] plan_request ok 0.1ms
    · [planner] availability_check ok 0.0ms
    · [tool_call] execute_plan ok 4133.2ms
      · [tool_call] document_search ok 4133.1ms
        ✗ [llm_call] rerank error 2041.2ms tokens=None+None error=ConnectionError
    · [evidence] evaluate_evidence ok 0.0ms
    ✗ [generation] answer_structured error 2047.7ms error=ConnectionError
      ✗ [llm_call] answer_structured error 2045.8ms tokens=None+None error=ConnectionError  <= failed here
```

这棵树本身就能说明发生了什么：检索阶段的 rerank 连不上模型，但 `rag.rerank` 捕获了异常并降级为未重排的候选，所以 `document_search` 仍然是 ok；证据判定通过之后，真正让请求中断的是生成阶段的模型调用。`failed_span_id` 指向的 span 里保存了完整的 traceback（已截断和脱敏）。客户端收到的是 500。

### 10.9 实现过程中发现的问题

- **R1（已修复）**：第一版实现是"第一个出现异常的 span 就是失败点"。在上面这个真实的失败请求里，它把已经被 `rag.rerank` 处理掉的 rerank 错误误判成了失败点（`failed_stage=tool_call`）。mock 测试没发现这个问题，因为测试里检索是 mock 的，根本没走到 rerank。修复后改为：在 run 失败时，按"导致中断的那个异常对象"匹配，并且要求这个异常逃出了该 span 的所有祖先。同时补了 API 层和 recorder 层两个回归测试。**修复之后重跑了全量测试、validation、A/B 基准和样例，§10.5–§10.8 的数字都来自最终代码。**
- **R2（已修复）**：检查 diff 时发现，recorder 在请求过程中执行的代码（`_trace_bundle`、`_prompt_facts`、`_record_response`）如果自己出错，会让请求失败。现在这几处都包在 `agent_trace.safely()` 里，并加了测试。

### 10.10 已知限制、风险和 Tech Debt

- **L1 · 未处理异常导致的 500 响应不带 `X-Run-Id`**（HTTPException 的 404/409 会带）。遇到这种情况，用 `python -m agent_trace list --status failed`，或按 session_id 和时间去 `trace_runs` 里查。
- **L2 · 没有保留期限，也没有清理机制**：API 的 Trace 会一直增长。基准测试里一共发了 880 个请求（其中 440 个开了 Trace，另外还有全部请求的会话记录），库文件是 4.1MB，也就是每个 API run 最多约 9KB。以后需要一个清理策略。
- **L3 · 进程被直接杀掉时**，run 会一直停在 `running`，没有 finished_at。这本身也是一个可以查到的事实。
- **L4 · 每个工具的 span 是事后重建的**：执行器在一次调用里跑完所有工具，所以工具 span 的 offset 是按执行器记录的单步耗时累加出来的（工具按顺序执行）；evidence span 的耗时是用总耗时减去各工具耗时推算的（`attributes.timing` 里标明了推算方式）。执行器运行期间发生的 llm_call 都挂在 `document_search` 下面，因为在当前代码里只有它会调用模型；**如果以后别的工具也开始调模型，这条归属规则就要改**（TD4）。
- **L5 · llm_call 的 name 是直接调用方的函数名**（通过 `sys._getframe` 取）；流式 llm_call 的 latency 包含了 consumer 在两个 delta 之间占用的时间。
- **L6 · 工具错误只有执行器给出的脱敏信息**（异常类名和固定的 message），这是执行器的设计决定的，本阶段按要求没有改执行器。
- **L7 · sanitizer 基于 key 名和值的模式匹配**：问题和答案里的业务文本不会被脱敏（这是有意的，因为要还原请求就需要它们）；Eval 的 Trace 保存了完整的 prompt 和证据（已 gitignore）。
- **L8 · Wiki 后台编译不在 Trace 范围内**，因为它不属于任何一个请求，而且不走 Provider。
- **L9 · 在 legacy 流式接口里**，"模型未返回可显示的答案"这个 RuntimeError 是在所有 span 之外抛出的，所以 `failed_stage=request`；orchestrated 流式的同一个检查放在 generation span 里面，因此会记为 generation。
- **L10 · `messages.trace_json`（客户端 trace）和新的 Trace 没有直接关联**：`messages` 表里没有 run_id，只能靠 session_id 和时间对上。
- **L11 · `git_commit` 在进程里只取一次**；代码改了但服务没重启时，记录的 commit 会过时。`git_dirty` 只看已跟踪的文件。
- **TD1**（沿用 Stage 0）：拆分 `llm_provider.py`。
- **TD2 · 写入开销的优化方向（本阶段不做）**：复用 SQLite 连接、把开始行和结束行合并成一次写入、改成后台异步写入、调整 checkpoint 策略。依据是 §10.7 的 cProfile 结论。
- **TD3 · `agent_trace.py` 大约 1000 行**，可以拆成 store、recorder、sanitize、provider 包装层和 CLI 几个部分。
- **TD4 · llm_call 的归属规则**（见 L4）：更准确的做法是在执行器里给每个工具开一个 span，但那需要改执行器。

### 10.11 补充验收（2026-09-24）

执行指令里列出的 10 项验收要求，逐条对照测试的结果如下。有两项原先覆盖不足，这次补了 3 个测试。**生产代码没有任何改动**，所以 §10.6 的 validation 回归和 §10.7 的 overhead 结果仍然对应 `8899d66`，有效，不需要重跑。

| # | 要求 | 测试（`tests/test_agent_trace.py`） |
|---|---|---|
| 1 | orchestrated 普通成功请求 | `test_plain_request_replays_every_stage_from_sqlite` |
| 2 | 多 Tool 成功请求 | `test_multi_step_tool_calls_are_recorded_in_order`、`test_wiki_and_document_route_records_both_tools` |
| 3 | Tool error 后正常拒答 | `test_tool_exception_is_an_error_span_in_a_completed_run` |
| 4 | Tool 异常导致请求失败 | `test_executor_programmer_error_fails_the_run_at_tool_call` |
| 5 | Generation / LLM 失败 | `test_model_failure_fails_the_run_at_generation`、`test_error_handled_inside_a_tool_is_not_blamed_for_a_later_failure`、`test_commit_conflict_fails_at_commit` |
| 6 | Streaming 成功与中途失败 | `test_streaming_run_uses_the_same_model`、`test_stream_failure_mid_generation_is_finalized`、`test_legacy_stream_is_traced`、`test_stream_closed_early_is_a_failed_run` |
| 7 | Legacy 路径 | `test_rule_routed_legacy_request`、`test_model_routed_legacy_request_records_the_router_llm_call`、`test_lock_contention_is_recorded_with_the_run_id_header`、`test_legacy_stream_is_traced` |
| 8 | Trace disabled | `test_trace_disabled_writes_nothing_and_changes_nothing`、`test_disabled_tracing_returns_a_null_run` |
| 9 | Trace 自身写库失败不影响业务 | `test_unwritable_trace_store_never_fails_the_request`（开始时写 run 失败）、**新增** `test_failed_finalize_write_never_fails_the_request`（结束时写库失败，流式和非流式都测；run 停留在 `running`，这本身也是一个可以查到的事实）、`test_a_bug_in_trace_recording_never_fails_the_request` |
| 10 | Trace 不包含 API Key 等敏感信息 | sanitizer 单元测试 3 个，以及**新增**端到端测试 `SecretLeakTests`：用带 Key 的 DeepSeek provider 发出真实形态的请求（先断言 Key 确实出现在发出去的 `Authorization` 头里），然后扫描两张表的全部内容——Key、问题里用户自己打出来的 Key、一个不是 `sk-` 格式、只能靠字面匹配脱敏的口令，都不出现，同时能看到 `[REDACTED]`；另一个用例里 provider 返回的 401 错误体里回显了 Key，error_message 和 traceback 中都已脱敏 |

测试结果：

```
.\.venv\Scripts\python.exe -X utf8 -m unittest discover
Ran 696 tests in 20.303s
OK
```

696 = 665 个原有测试 + 31 个 Trace 测试。本节和补充的测试在同一个 commit 里。

按要求在这里停止，没有进入 Stage 2。

---

## 11. Stage 2 — Diagnostic Eval

- 分支：`stage0-llm-provider`（本地，**未 push**）
- 实现 commit：`3f1ff974b2ff8c1c417999938f0d4e552b633bae`
- 本节所在的 docs commit 在它之后
- 目标：基于 Stage 1 持久化的 Trace，把最终的 pass/fail 拆成可以解释的阶段级诊断，并找出每个失败 case 的最早根因
- 按要求在此停止，**没有进入 Agent 优化阶段**；诊断出来的问题一律没有修

### 11.1 决策是怎么落实的（对照你给的 9 条约束）

1. **overlay labels**：新标签放在 `eval/diagnostic_labels/validation_v1.labels.json`，**冻结的数据集没有改**（sha256 仍是 `e4ad670c…`）。overlay 锁定了数据集的 sha256，一旦对不上就拒绝加载。
2. **routing 只判断高层 route**（`route_acceptable`：route 是否在 `acceptable_routes` 中）。steps、required/forbidden tools、signals、arguments、availability **全部归 planning**，两个阶段的职责没有重叠。
3. **自由文本的 notes 从不被解析**。推导只用结构化字段（expected_behavior、expected_route、required_source_types、expected_fact_groups/patterns、expected_message、question）。notes 只作为我人工编写 overlay 的依据；没有可靠标签的检查允许输出 undetermined。有测试 `test_notes_are_never_parsed_into_labels` 覆盖。
4. **拒答机制不符**：上游都通过时，归为 `evidence_error`。如果 policy 的拒答是由某个 plan signal 引起的（`freshness_unsupported`→`requires_freshness`，`exact_citation_missing_*`→`requires_exact_citation`，这个对应关系来自 `evidence_policy.py` 的源码），那么：
   - 该 signal 被标注为错误 → planning 先失败，primary 是 `planning_error`，evidence 这一项记为 secondary；
   - 该 signal 被标注为正确 → primary 是 `evidence_error`；
   - 该 signal 没有标注 → `label_gap`。
5. **LLM Judge 只保留了可插拔接口**（`SemanticJudge`）。默认的 `DisabledJudge` 从不调用模型，只返回 `rule_inconclusive`；报告里记录 judge 名称和调用次数（这次是 0）。每个检查都带有 `method`（rule / judge），将来如果接入 judge，靠 judge 判定的 primary 会单独统计（`primary_by_method`）。
6. **规则无法证明是 generation 的错时，不归为 generation_error**。事实没有命中但答案也不是拒答（可能是同义改写）、应该拒答的问题却没有出现拒答标记，这两种情况都会进入 `unattributed: rule_inconclusive`。
7. **`unattributed` 不是第七类错误**，只用来记录 `missing_trace`、`environment_failure`、`rule_inconclusive`、`label_gap` 这几种诊断缺口，在报告里单独列一张表。
8. **primary_error 只统计有确定性证据支持的最早根因**：要求最早失败的那个阶段由规则判定，并且它之前的所有阶段都是 pass 或 not_applicable。secondary effects 和 latent issues 分别单独统计，不计入 primary。
9. **blind_v2 一个字节都没有读**。`load_labels` 在读取文件之前，就会按文件名拒绝任何包含 blind 的数据集（有测试覆盖）；代码里没有任何地方引用 blind_v2 的路径。

### 11.2 Eval Case 的新 schema，以及怎么兼容现有 40 条

生效的标签 = 从冻结数据集推导出来的标签 + overlay 的补充（只能补充，和原字段矛盾时报错）。

| 标签 | 来源 | 规则 |
|---|---|---|
| `expected_outcome` / `refusal_mechanism` | 推导 | answer→answer；generation_refuse→refuse + generation；policy_refuse→refuse + policy；boundary→boundary |
| `acceptable_routes` | 推导，overlay 可以放宽 | 默认是 `[expected_route]`；放宽时必须仍然包含原来的 expected_route |
| `required_tools` | 推导 | 优先从 required_source_types 推；如果为空，则从 expected_route 的 steps 推（拒答前也必须先查过对应通道）；boundary 类为空（在执行工具之前就应该给出固定答案） |
| `expected_message`、`facts`、`citation_required` | 推导 | 原样沿用；事实匹配直接复用评测器的 `evaluate_facts` 和 `body_for_fact_matching`，和官方评分口径一致 |
| `forbidden_tools`、`plan_constraints`、`expected_arguments`、`expected_tool_status`、`expected_evidence` | 只能来自 overlay | 人工编写，每条都附有 rationale |
| `wiki_corpus`（overlay 级别） | overlay | 声明 wiki 页面标签是针对哪份语料写的（`committed_sample` 或 `published_build:<id>`） |

overlay 目前的覆盖情况：40 条 case 里有 24 条有 overlay 标签。其中 `expected_evidence` 20 条（依据是 notes 里写明的章节或页面，以及 system fixture 里的 SKU）、`expected_arguments` 10 条、`expected_tool_status` 4 条（不存在的 SKU 应返回 empty）、`plan_constraints` 1 条（h008）。剩下 16 条（缺失信息类拒答和 boundary 类）只用推导出来的标签。

**标签偏差说明（需要你复核）**：overlay 完全是按 notes 和语料写的，写的时候没有看 Trace。但 h008 的 Trace 我在 Stage 1 时看到过，也在 Stage 2 的方案里分析过。它那一条标签（`requires_freshness=false`）的依据是 notes 中"考察时效词是否导致对可回答制度问题误拒"这句话，理由写在 overlay 的 rationale 里。**这条标签单独决定了 h008 的诊断结论**（见 §11.6 的敏感性对照），建议你亲自确认一下。

### 11.3 Trace → 诊断的映射（全部是确定性规则）

| 阶段 | 读取的 span | 检查项（任意一项 fail 即该阶段 fail） |
|---|---|---|
| routing | `planner/plan_request` | `route_acceptable` |
| planning | `plan_request`、`availability_check`、工具 span 的 arguments | `planner_completed`、`required_tools_planned`、`forbidden_tools_absent`、`signal:<name>`、`availability_boundary_message`（boundary 类）/ `availability_not_short_circuited`（其他类；如果语料不可用，记为 environment_failure）、`argument:<tool>.<key>` |
| tool | `execute_plan` 及其子 span | `executor_completed`、`executed:<tool>`（包括非必需工具报错；只是必需工具不在计划里的话，归 planning 管） |
| retrieval | 工具 span 的 `output.evidence` | `status:<tool>`（例如不存在的 SKU 应返回 empty）、`expected_evidence:<type>`（document 按 `## 标题` 匹配，wiki 按 `page_title`，system 按 locator 里的 SKU）、`answer_facts_retrieved`（answer 类：检索结果里是否包含答案事实） |
| evidence | `evidence/evaluate_evidence` | `policy_outcome`（按 §11.1 第 4 条的规则判定）、`required_sources_usable`、`answer_evidence_kept`（检索到的关键证据有没有进入 usable 列表）；如果 policy 是因为工具结果而拒答，并且上游工具确实失败或返回空，这一阶段记为 blocked（policy 反应正确，失败归工具那一层） |
| generation | `generation` span | `generation_completed`、`not_a_restatement`、`no_false_refusal`（只有在 usable 证据里确实有答案时才判 fail）、`citation_present`、`citation_indices_valid`、`answer_facts` / `refused`（规则判不出来时交给 judge，v1 的结果是 inconclusive） |

阶段状态有五种：`pass | fail | inconclusive | blocked | not_applicable`。检查项另外有一种 `skipped`，表示这条标签不适用于本次运行的环境，不参与统计。

如果 run 本身异常中断，按 `failed_stage` 映射到对应的阶段；commit 或 request 阶段的异常则记为 `environment_failure`。

### 11.4 primary root cause 的判定顺序

1. 官方结果判 pass：不给出 primary；如果有阶段 fail，就记为 `latent_issues`。
2. 没有 Trace → `missing_trace`；run 在 Agent 各阶段之外中断 → `environment_failure`。
3. 依次检查 routing → planning → tool → retrieval → evidence → generation：
   - 第一个 `fail` 的阶段 = primary（同时记录 method），之后各阶段的 fail 记为 secondary；
   - 如果先遇到 `inconclusive`，就记为 unattributed（按它的 gap 类型：label_gap、rule_inconclusive 或 environment），之后的 fail 仍然列出来，但不计入 primary；
   - `pass`、`not_applicable`、`blocked` 继续往后检查。
4. 所有阶段都没有 fail，但官方结果判 fail → `rule_inconclusive`（说明规则有缺口）。

### 11.5 改动的文件（`3f1ff97`）

| 文件 | 说明 |
|---|---|
| `diagnostic_eval/labels.py` | 标签推导、overlay 的加载和校验（包括 sha、未知 case、字段矛盾、wiki 语料声明、拒绝读取 blind 数据集） |
| `diagnostic_eval/rules.py` | `TraceView`、6 个阶段的检查、`SemanticJudge` 和 `DisabledJudge`、`diagnose()` |
| `diagnostic_eval/report.py` | `diagnose_eval()`（从 eval 文件记录的环境得出 wiki 语料信息）、聚合统计、Markdown 报告 |
| `diagnostic_eval/__init__.py`、`__main__.py` | 包入口和 CLI：`python -m diagnostic_eval --eval eval/<label>.json --labels <overlay>` |
| `eval/diagnostic_labels/validation_v1.labels.json` | 人工编写的 overlay |
| `eval/diagnostics/stage1_trace_qwen.diagnostic.json` / `.md` | 在 Stage 1 的真实 Trace 上跑出来的诊断报告（JSON 是 case 级明细，469KB） |
| `tests/test_diagnostic_eval.py` | 44 个测试 |
| `eval/README.md` | 补充诊断产物的说明 |

**没有改**：`agent_trace.py`、`evaluate_answerability.py`（只 import 它的函数）、冻结的数据集、Agent 相关代码、Prompt、Retriever、业务决策逻辑。已跟踪文件的 diff 为 0。

### 11.6 结果：validation_v1（`stage1_trace_qwen`，120 个 case-run，离线诊断，没有重跑 Agent）

```
.\.venv\Scripts\python.exe -X utf8 -m diagnostic_eval --eval eval\stage1_trace_qwen.json --labels eval\diagnostic_labels\validation_v1.labels.json
case runs 120: passed 117, failed 3
primary: {'planning_error': 3}
unattributed: none
secondary: {'evidence_error': 3}
latent: none
```

**聚合的 root cause 分布**

| category | primary | secondary effects | latent issues |
|---|---:|---:|---:|
| routing_error | 0 | 0 | 0 |
| planning_error | **3** | 0 | 0 |
| tool_error | 0 | 0 | 0 |
| retrieval_error | 0 | 0 | 0 |
| evidence_error | 0 | 3 | 0 |
| generation_error | 0 | 0 | 0 |

unattributed 四类（missing_trace / environment_failure / rule_inconclusive / label_gap）都是 0。

**各阶段状态（全部 120 个 case-run）**：routing 120 pass；planning 117 pass / 3 fail；tool 96 pass / 24 n/a；retrieval 72 pass / 48 n/a；evidence 93 pass / 3 fail / 24 n/a；generation 81 pass / 3 blocked / 36 n/a。

**case 级诊断**（3 次运行的结论一致）：

```
answer_document_h008  run 1-3  primary=planning_error  method=rule
  routing pass · planning FAIL · tool pass · retrieval pass · evidence FAIL · generation blocked
  primary:   signal:requires_freshness — requires_freshness=True, labelled False
  secondary: evidence_error (policy_outcome) — policy refuse with ['freshness_unsupported']
```

问句"目前的制度里，核心协作时间是几点到几点？"中的"目前"让 Planner 设置了 `requires_freshness`，Evidence Policy 因此判定 `freshness_unsupported` 并拒答，模型根本没有被调用。检索其实已经拿到了包含答案的「工作时间」章节（retrieval pass）。

**敏感性对照**：去掉 overlay 再跑一次，3 条都变成 `unattributed: label_gap`，原因是"拒答来自 plan signal `requires_freshness`，但没有标签"。这说明在缺少标签时，诊断会拒绝猜测；也说明 h008 的结论完全取决于那一条人工标签（见 §11.2 的偏差说明）。

### 11.7 测试（`tests/test_diagnostic_eval.py`，44 个，全部 mock，不联网）

Trace 用 `agent_trace` 真实的 Run API 写进临时 SQLite，再用 `load_trace` 读回来做诊断。

| 覆盖面 | 测试 |
|---|---|
| routing_error | 高层 route 错误；boundary 问题被路由到文档；`acceptable_routes` 放宽后不再误报 |
| planning_error | 错误的 signal（evidence 为 secondary，generation 为 blocked）；错误的 SKU 参数；错误的 boundary 消息；route 被接受但缺少必需工具 |
| tool_error | 必需工具报错（retrieval 为 secondary，evidence 为 blocked）；执行器中断 |
| retrieval_error | 没有检索到期望的章节；不存在的 SKU 却返回了数据；没有 evidence 标签时，靠事实检查判定 |
| evidence_error | policy 拒绝了正确的证据；拒答机制不符；关键证据被丢弃；signal 标注为正确时，policy 成为根因 |
| generation_error | usable 里有答案却拒答；缺少引用；引用编号越界；生成异常；只是复述问题 |
| unattributed | missing_trace；signal 没有标注（label_gap）；**无法证明的 generation 失败不归为 generation_error**；应拒答却没有拒答标记；工具上游正常时的工具类拒答理由；语料不可用；Agent 阶段之外的异常；官方判 fail 但没有规则能解释 |
| 统计与 judge | primary、secondary、latent、gap 分开计数；disabled judge 让语义问题保持 inconclusive；外接 judge 的结论标为 judge；规则能判定时不调用 judge |
| 标签与兼容 | 40 条 case 都能得到标签，overlay 生效；notes 不会被解析；数据集 sha 没变；overlay 被篡改、有未知 case 或字段矛盾时报错；拒绝读取 blind；wiki 语料不一致时跳过对应检查、不判 fail |
| 集成 | 真实 `chat_orchestration.prepare` 的成功 Trace 在 6 个阶段全部 pass；**真实 Planner 的 freshness 问题被诊断为 planning_error**；端到端生成报告 |

```
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_diagnostic_eval
Ran 44 tests in 3.670s
OK
.\.venv\Scripts\python.exe -X utf8 -m unittest discover
Ran 740 tests in 23.261s
OK
```

740 = 696（Stage 0 和 Stage 1 的全部测试）+ 44。

### 11.8 开发过程中发现的问题

- **F1 · 评测环境和 case 的描述对不上（wiki 语料漂移）**。validation_v1 的 wiki case 在 notes 里引用的是**仓库里提交的样例 Wiki**（4 页，其中有「请假与年假」），但 eval 实际读取的是 `data/wiki` 里已发布的 **build-0001**（20 页，按文档章节编译而成，没有「请假与年假」）。第一次诊断时，这导致 `answer_wiki_h003` 出现了 3 条误报的 latent `retrieval_error`。我没有照着 Trace 去改标签，而是让 overlay 声明 `wiki_corpus: committed_sample`，诊断时读取 eval 文件记录的环境（`published_build_id`）做比对：两者不一致时，wiki 页面标签标记为 `skipped`（18 个 case-run），wiki 类的检索改为只看答案事实是否被检索到。**这意味着到目前为止，所有 wiki case 的评测结果都是在另一份语料上得出的。**这一点值得单独决定怎么处理，本阶段不做改动。
- **F2 · Tool 失败之后，policy 以 `tool_error` 为由拒答，这是正确的反应**。第一版规则会把它当成 evidence 阶段的问题，现在记为 evidence `blocked`，失败只归 tool 这一层。
- **F3 · 模块和函数同名**：`diagnostic_eval.diagnose` 既是子模块又是导出的函数，导致导入混乱，已把子模块改名为 `rules.py`。

### 11.9 未解决的问题和风险

- **R1 · 这次的失败分布几乎没有信息量**：validation_v1 上只有 1 个 case 失败，而且这个集合已经被看过。结论只能说明诊断链路可以跑通，**不能作为能力指标**。要拿到有意义的分布，需要一个没被看过的数据集（按要求，本阶段不读 blind_v2）。
- **R2 · 标签依赖人工**：`plan_constraints` 只有 1 条，`expected_arguments` 和 `expected_evidence` 是按 notes 写的。标签越少，`label_gap` 就越多；标签写错，结论就会跟着错（h008 就是一个例子）。
- **R3 · 用子串匹配事实可能误判为通过**：遗留的 `expected_fact_groups` 是子串匹配（例如 "10"），检索阶段的"答案事实已检索到"可能被无关的章节碰巧满足。它只会造成误判通过，不会造成误判失败；另外 `expected_evidence` 标签可以进一步约束。
- **R4 · reason → signal 的对应关系是手工同步的**（`SIGNAL_FOR_REASON`）。如果 `evidence_policy.py` 新增了由 signal 触发的 reason，而这里没有同步，对应的拒答会落入 `rule_inconclusive`，不会被误判成某一类错误。
- **R5 · legacy 模式的 API Trace 没有诊断**：Eval 只走 orchestrated 链路，legacy 模式也没有标签。
- **R6 · judge 没有启用**：凡是需要语义判断的 generation 情况都会留在 `rule_inconclusive`。这次运行里没有出现这种情况。
- **R7 · 诊断是离线的，依赖 `traces.sqlite`**：Trace 库在 gitignore 的 artifacts 目录里。已提交的诊断报告里带有 span_id，但重新生成报告需要本地的 Trace 库。
- **TD5 · 诊断报告 JSON 有 469KB**：每一项检查都带着期望值和实际值。如果以后数据集变大，可以提供一个精简模式。

---

## 12. Stage 2.1 — 可复现的 Eval Environment

- 分支：`stage0-llm-provider`（本地，**未 push**）
- 目标：冻结一个可复现的 Eval Environment，解决 validation case、Wiki 语料和实际运行的 build 三者不一致的问题
- 按要求在此停止，**没有修改 Agent 的任何行为**

| commit | 内容 |
|---|---|
| `2fb055a3cae6bd4d6874b3dfc1dac069f9a3d97e` | feat：`eval_env` 包（make / verify / activate / run / diff）、测试，以及 `diagnostic_eval` 读取固定的 wiki 语料 |
| `9ba92c709ce5b5c60aea02f243140090ba8e91a8` | fix：生成环境时，在创建 staging 目录之前就记录 git 状态（见 §12.9 F1） |
| `38afa41a3b5318a8e51b47e92055ec5460f373cb` | eval：不可变环境 `eval-env-v1` |
| `adaf8d89659456ac36db72d3f089869e350af151` | feat：环境 diff 里增加"同一侧多次运行中出现了几种答案"和"是否用到 wiki"这两个事实字段 |
| `ffdac42170c5049a70ec4dfb3fb31f534ea1cbcc` | eval：Env V1 baseline、诊断报告、环境 diff |
| 本节所在的 docs commit | HANDOFF §12 |

### 12.1 决策是怎么落实的（对照你给的 10 条）

1. **eval-env-v1 用的是提交的 4 页样例 Wiki**（`corpus_id: committed_sample`），和 validation_v1 的原始设计、V1 评测报告一致；**没有**建 build-0001 的第二个环境。机制本身支持 `published_build` 类型（锁定 build_id 和内容 hash，运行时读的是快照），有测试覆盖，但这次没有使用。
2. **document、wiki、system 三份语料都复制进环境**，作为不可变快照；`make` 拒绝覆盖已经存在的环境。
3. **tree 有改动时默认拒绝执行**，而且新增的未跟踪文件也算改动。`--allow-dirty` 只能用于 exploratory 运行：结果 metadata 里会标记 `run_kind: exploratory`、`baseline_eligible: false`，并且这种运行不允许用含 "baseline" 的名字。
4. **用 env-v1 跑了 Qwen 3 轮 validation 和 Diagnostic Eval**，形成新的 Env V1 baseline（§12.6–§12.7）。和旧结果的差异一律归因为 environment change（§12.8）。
5. **新 harness（`eval_env/common.py`）完全自包含**，没有 import `run_stage0_eval.py`，那个 Stage 0 历史脚本一字未改。
6. **诊断 overlay 也复制了一份快照进环境**，并锁定 hash（`labels/validation_v1.labels.json`）；冻结的数据集仍然用路径加 sha256 引用。校验时还会检查：这份 overlay 是针对哪个数据集版本写的，以及它声明的 `wiki_corpus` 和环境是否一致。
7. **Embedding 用环境专属的缓存**，存放在 `eval/artifacts/env-cache/<env_id>/embeddings-<key>.json`，缓存 key 绑定环境 id、embedding 模型 digest 和切块指纹，旁边的 `.meta.json` 记录它的来源；来源对不上就删除重建。共享的 `.cache/embeddings.json` 在运行期间被指向一个空路径，不会被读到。
8. **metadata 额外记录了 Python、Ollama、platform 的版本**，只记录，不作为执行条件。
9. **Retriever 参数只记录**（`retriever.config` 和它的 `config_sha256`），**不冻结进环境**；manifest 里的 `not_pinned` 写明了这一点。
10. **没有修改 Agent 的行为，没有读取 blind_v2**（按文件名拒绝，make 和 verify 都有测试），**没有覆盖 Stage 0–2 的任何历史结果**：从 Stage 2 结束（`2d74f0d`）到现在，唯一被修改的已有文件是 `diagnostic_eval/report.py`，而且是代码，不是结果文件；另外 `run` 本身也拒绝覆盖任何已存在的输出。

### 12.2 Env manifest（`eval/environments/eval-env-v1/manifest.json`，sha256 `a6ecd2b4ae9dbede1285ece99da8fd840b300665ef5a882df0c642d30cc0c78f`）

```json
{
  "schema_version": 1,
  "env_id": "eval-env-v1",
  "description": "validation_v1 as originally designed: the committed 4-page sample wiki (not the locally compiled data/wiki build), the sample rulebook and the sample business fixture.",
  "created_from": {"commit": "9ba92c709ce5b5c60aea02f243140090ba8e91a8", "describe": "stage0-baseline-11-g9ba92c7", "branch": "stage0-llm-provider", "dirty": false},
  "dataset": {"path": "eval_answerability_validation_v1.json", "sha256": "e4ad670c6dd6512c8ffcf647b967861d9d1bbef44dbc7872d3cfbf96915c6ced"},
  "diagnostic_labels": {"path": "labels/validation_v1.labels.json", "sha256": "6cacbcb98b3d708f3ec0d2188e788de826e95fdf4b995d6e65bfb913f1acc44e", "copied_from": "eval/diagnostic_labels/validation_v1.labels.json"},
  "corpus": {
    "documents": [{"path": "corpus/documents/sample_company_rules.md", "source_name": "sample_company_rules.md", "sha256": "c30634966afdaa5803ce9d4db71d3a3e32d8ede1f2ab59fe15f0645ca4d11704"}],
    "wiki": {"kind": "pages_file", "corpus_id": "committed_sample", "path": "corpus/wiki/sample_company_wiki.json", "sha256": "b6eac725e2dbd4191a5c7dea017e774cd32fde5621ce5968d5d450f0f07a58e7", "page_count": 4},
    "system_fixture": {"path": "corpus/system/sample_business_system.sql", "sha256": "843bad7499a246d428d19bc75a1a44b6cebbc59d646fb593ac074b4e1810c184"}
  },
  "embedding": {"model": "nomic-embed-text", "ollama_digest": "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"},
  "not_pinned": {"retriever": "experiment configuration - recorded per run, not part of the environment", "chat_model": "the system under test - recorded per run"}
}
```

（上面省略了 `created_at`，以及每个文件的 `bytes` 和 `copied_from`，完整内容见原文件。）

### 12.3 执行前的校验（`verify`，任何一项不通过都会拒绝执行，一共 18 项）

schema 版本；env_id 和目录名一致；数据集文件存在、sha 一致，且不是 blind；标签快照存在、sha 一致；标签针对的数据集版本正确；标签声明的 `wiki_corpus` 和环境一致；只有一份文档，且文档快照存在、sha 一致；wiki 快照存在、sha 一致，页数一致（published_build 类型还要校验 build_id）；system fixture 存在、sha 一致；Ollama 上的 embedding digest 一致（Ollama 连不上也会拒绝）；tree 是干净的。运行结束后还会再校验一次，确认运行没有改动快照（`post_run_manifest_sha256`）。

### 12.4 运行 metadata（`eval/env_v1_validation_qwen.json` 的 `environment` 部分）

| 项 | 本次的值 |
|---|---|
| 运行类型 | `run_kind: reference`，`baseline_eligible: true` |
| 环境 | `eval-env-v1`，manifest `a6ecd2b4…`，运行后仍为 `a6ecd2b4…`，18 项校验全部通过 |
| 数据集 / 标签 | `e4ad670c…` / `6cacbcb9…`（环境内的快照） |
| 语料 | 文档 `c3063496…`；wiki 为 `pages_file / committed_sample / b6eac725… / 4 页`；fixture `843bad74…` |
| Embedding | `nomic-embed-text`，digest `0a109f42…`；chunk 20 个；切块指纹 `7baada00…`；**索引指纹** `9aadd281…`；使用环境专属缓存（这次是首次构建） |
| Retriever | 配置本身，以及 `config_sha256` `16acc9cc…`（只记录） |
| Provider / model | ollama / `qwen3:4b`，digest `359d7dd4…`，Q4_K_M，权重 blob `3e4cb141…` |
| 代码 | git `38afa41`，dirty 为 false；关键代码文件的 sha256 |
| 运行时版本 | Python 3.12.14 / Ollama 0.32.15 / Windows-11-10.0.26200 |
| Trace | `eval/artifacts/env_v1_validation_qwen/traces.sqlite`：120 个 run 全部 completed，753 个 span；每个 case-run 都有 `trace_run_id` |

### 12.5 改动的文件

| 文件 | 说明 |
|---|---|
| `eval_env/environment.py` | 生成快照（make）、校验（verify）、运行时接入（activate）、和环境绑定的 embedding 缓存、切块指纹和索引指纹 |
| `eval_env/common.py` | 自包含的 harness 公共逻辑（请求监听、模型信息、Retriever 配置、Trace 安装、case 整理、git 状态和运行时版本） |
| `eval_env/__main__.py`、`__init__.py` | CLI：`make`、`verify`、`run`、`diff` |
| `diagnostic_eval/report.py` | `eval_context` 优先读取 `eval_environment.corpus.wiki.corpus_id`，旧的结果文件仍走原来的回退逻辑 |
| `tests/test_eval_environment.py` | 22 个测试 |
| `eval/environments/eval-env-v1/**` | 不可变的环境（manifest 加 4 个快照文件） |
| `eval/env_v1_validation_qwen.json`、`eval/diagnostics/env_v1_validation_qwen.diagnostic.{json,md}`、`eval/diagnostics/env_v1_validation_qwen_vs_stage1_trace_qwen.environment_diff.{json,md}` | baseline、诊断报告、环境 diff |

**没有改**：Agent 相关代码、`agent_trace.py`、`evaluate_answerability.py`、`run_stage0_eval.py`、冻结的数据集、`eval/diagnostic_labels/` 下的原始 overlay、Stage 0–2 的全部结果文件。

### 12.6 测试

`tests/test_eval_environment.py` 共 22 个测试，Ollama 和 git 都是 mock 的：

- **make**：快照了所有输入并且能通过校验；Retriever 不在 manifest 里；git 状态在 staging 之前读取；环境不可变；拒绝针对别的数据集写的标签（被拒绝时什么都不会留下）；拒绝 blind。
- **verify**：文档快照、标签快照或数据集被改动时拒绝；embedding digest 不一致或 Ollama 连不上时拒绝；tree 有改动时拒绝，加了 `--allow-dirty` 就变成 exploratory 且 `baseline_eligible=false`；exploratory 运行不能用含 baseline 的名字；拒绝覆盖已有结果；目录改名后拒绝。
- **published_build**：快照被锁定，并且运行时读到的就是快照里的页面；build_id 不一致时拒绝；标签的 wiki 语料和环境不一致时拒绝。
- **activate**：本机即使有一个会覆盖默认 Wiki 的"活"build，环境内读到的仍然是 4 页快照；文档、fixture 和 embedding 缓存的路径都指向环境；退出后全部恢复原样。embedding 缓存按环境、digest 和切块绑定：条件相同就复用，digest 一变就重建；来源被伪造的缓存会被删掉。
- **diff 和诊断**：诊断会读取固定的 wiki 语料，旧文件走回退逻辑；环境 diff 把每一处差异都归为 environment_change。

```
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_eval_environment
Ran 22 tests in 1.012s
OK
.\.venv\Scripts\python.exe -X utf8 -m unittest discover
Ran 762 tests in 24.966s
OK
```

762 = 740（Stage 0–2 的全部测试）+ 22。

### 12.7 Env V1 baseline（validation_v1，Qwen，3 轮）

```
.\.venv\Scripts\python.exe -X utf8 -m eval_env verify --env eval-env-v1
eval-env-v1 verified: 18 checks, manifest sha256 a6ecd2b4…, run kind reference
.\.venv\Scripts\python.exe -X utf8 -m eval_env run --env eval-env-v1 --label env_v1_validation_qwen --runs 3
```

| 指标 | Env V1 |
|---|---|
| 每轮通过数 | 39 / 39 / 39 |
| Answer Success / False Refusal | 95.0% / 5.0% |
| Unanswerable Refusal / Boundary | 100% / 100% |
| Citation Presence / Index Validity | 100% / 100% |
| Required Source Coverage / Fact Hit | 100% / 95.0% |
| 跨轮稳定性 | 100%（39 题 3/3 通过，1 题 0/3） |
| 耗时 p50 / p95 / max | 2.82 / 10.35 / 11.15 秒 |
| 7 项门禁 | 全部通过 |

**Diagnostic Eval**（使用环境内的标签快照）：
```
.\.venv\Scripts\python.exe -X utf8 -m diagnostic_eval --eval eval\env_v1_validation_qwen.json --labels eval\environments\eval-env-v1\labels\validation_v1.labels.json
case runs 120: passed 117, failed 3
primary: {'planning_error': 3}   unattributed: none   secondary: {'evidence_error': 3}   latent: none
```
- 3 个失败的 case-run 都是 `answer_document_h008`，primary 是 `planning_error`（`requires_freshness=True`，而标签是 False），secondary 是 `evidence_error`（`freshness_unsupported`）。
- **不再有被跳过的检查**：Stage 2 那次运行里，wiki 页面标签因为语料不一致被跳过了 18 个 case-run；这次运行读的正是标签所针对的那份 Wiki，所以 wiki 页面检查全部适用，并且全部通过（retrieval 阶段 72 pass、0 fail）。

### 12.8 旧环境 → 新环境：逐 case 差异（`env_v1_validation_qwen_vs_stage1_trace_qwen.environment_diff.md`）

> 按规则，每一处差异都归因为 environment change，不判断为回归或改进。

- 旧环境：`stage1_trace_qwen`（commit `5baf106`），wiki **没有固定**，读的是本机 `data/wiki` 里发布的 build-0001（20 页）。
- 新环境：`eval-env-v1`（commit `38afa41`），wiki 是 `committed_sample`（4 页）。
- 每轮通过数：旧 `[39, 39, 39]` → 新 `[39, 39, 39]`。**40 个 case 的通过次数全部相同**，行为和路由也全部相同。
- 11 个 case 的答案文本有变化，其余 29 个完全相同。

| case | 通过次数（旧 → 新） | 本侧 3 次运行里的不同答案数（旧 / 新） | 用到 wiki |
|---|---|---|---|
| `answer_wiki_h001` | 3 → 3 | 1 / 2 | 是 |
| `answer_wiki_h002` | 3 → 3 | 1 / 2 | 是 |
| `answer_wiki_h003` | 3 → 3 | 1 / 1 | 是 |
| `answer_wiki_h004` | 3 → 3 | 1 / 1 | 是 |
| `answer_multi_h003` | 3 → 3 | 1 / 1 | 是 |
| `answer_multi_h004` | 3 → 3 | 1 / 2 | 是 |
| `answer_document_h001` | 3 → 3 | 2 / 1 | 否 |
| `answer_document_h005` | 3 → 3 | 1 / 2 | 否 |
| `answer_document_h007` | 3 → 3 | 1 / 1 | 否 |
| `answer_multi_h002` | 3 → 3 | 1 / 2 | 否 |
| `refuse_missing_h007` | 3 → 3 | 1 / 2 | 否 |

**供阅读时参考的事实**（不改变上面的归因）：
- 6 个用到 wiki 的 case，读到的 Wiki 页面本身就不同，所以答案文本变了，这是意料之中的。
- 另外 5 个没有用到 wiki 的 case：embedding **不是**原因，环境专属缓存里的 20 个向量和旧的共享缓存逐位相同（差值为 0）；而且这些 case 的答案文本在**同一个环境**里本来就会变，例如 env-v1 自己的 3 次运行里，就有 3 个 case 出现了两种措辞（结尾标点、有没有"根据资料"这类前缀），旧环境两次运行之间（Stage 0 的 baseline 和 regression）本来也有 3/40 个 case 措辞不同。

### 12.9 发现的问题

- **F1（已修复）· 生成环境时误记了 `dirty: true`**：`make` 在创建 staging 目录之后才读 git 状态，把自己的临时目录当成了未跟踪文件。当时那份环境还没有被提交或使用，所以修复后删掉重新生成了（`9ba92c7`，并加了测试）；现在 manifest 里的 `created_from` 是干净的 `9ba92c7`。
- **F2 · Stage 0–2 的所有结果都是在一份没有固定的 Wiki 上跑出来的**（本机 `data/wiki` 的 build-0001），而那份 Wiki 取决于这台机器上传过什么文档、编译出了什么。从 Env V1 开始，Eval 不再受本机 `data/wiki` 影响。

### 12.10 未解决的问题和风险

- **R1 · `run_stage0_eval.py` 仍然会读本机的 `data/wiki`**。它是 Stage 0–1 的复现脚本，按要求保持原样，**以后的新 Eval 一律用 `python -m eval_env run`**。
- **R2 · 环境只固定数据，不固定代码和模型**：代码由 git commit 和代码 hash 追溯，被测的 chat 模型只做记录。要比较两个 Agent 版本，必须在同一个环境里各跑一次。
- **R3 · embedding digest 的校验依赖 Ollama 在线**：Ollama 不在线时会直接拒绝执行，而不是跳过这项校验。
- **R4 · 环境专属的 embedding 缓存在 gitignore 的 artifacts 目录里**，换一台机器就会重新计算。可以用索引指纹来比对两台机器算出来的向量是否一致。
- **R5 · 同一环境里答案文本本来就会波动**（例如 rerank 没设 temperature），所以只看文本做 diff 会显示"有变化"。以后比较两次运行时，应该看通过数、行为和诊断，而不是答案文本。
- **R6 · 数据集是按路径加 sha 引用的，没有复制进环境**：如果仓库里的这个文件被删掉或改动，这个环境就会拒绝执行，这是有意的。
- **R7 · API（产品）本身仍然使用本机的 `data/wiki`**，这是产品设计，本阶段不改。环境固定只作用于 Eval。
- **R8 · 只有一个环境**：没有为 build-0001 建环境。如果以后想在编译出来的 Wiki 上做评测，需要为它单独写一份 overlay，并在 `published_build` 类型的环境里运行。

---

## 13. Stage 2.5 — Qwen vs DeepSeek Diagnostic Baseline

- 分支：`stage0-llm-provider`（本地，**未 push**）
- 本阶段只做模型对照实验。**没有修改** Agent、Prompt、Retriever、Diagnostic Eval、`eval_env` 或 eval-env-v1；没有读取 blind_v2；没有根据结果去改 Agent。

| commit | 内容 |
|---|---|
| `58f2611759133f7be41706b05033382d14276e92` | 实验 runner（`eval/model_comparison.py`）及其测试；两组实验都在这个 commit 上运行 |
| `75cce9295278ee2c9e256f9171bb2684d87efb2e` | 实验结果：两组的 eval 结果、诊断报告、对照报告 |
| 本节所在的 docs commit | HANDOFF §13 |

### 13.1 实验控制

- **环境**：eval-env-v1（manifest `a6ecd2b4…`），整个实验只做**一次**干净 tree 的校验。进入第二组之前，runner 会检查 tree 上的变化是不是**只有**第一组自己的输出文件，否则中止实验。
- **两组完全相同的部分**（逐项核对过）：git commit `58f2611`，dirty 为 false；Agent 代码的 sha256；Retriever 配置 hash；**索引指纹**（两组用的是同一批 embedding 向量，而且和 Env V1 baseline 一致）；数据集、语料、标签、embedding digest。
- **唯一的变量**：provider/model。一组是 `ollama / qwen3:4b`（digest `359d7dd4…`，Q4_K_M），另一组是 `deepseek / deepseek-flash`（响应里返回的 model 也是 `deepseek-flash`）。**DeepSeek 的思考模式由 Provider 固定关闭**。
- **切换方式**：设置 `LLM_PROVIDER` 并调用 `llm_provider.reset_provider()`；`rag.CHAT_MODEL` 这个记录用的常量也同步改成对应的模型名。
- **成本数据**：runner 在最外层包了一层 `requests.post`，记录每次 chat 调用的原始 usage（包括 DeepSeek 的 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`），以及调用时间。价格取自官方文档（2026-09-24 查证）：deepseek-flash 非高峰时段每 1M token，输入缓存命中 $0.003、未命中 $0.15、输出 $0.6，高峰时段价格翻倍。
- **运行顺序**：Qwen 3 轮（05:13–05:21 UTC），然后 DeepSeek 3 轮（05:21–05:25 UTC）。

### 13.2 Qwen 3 轮结果（`eval/stage25_qwen_env_v1.json`）

| 指标 | 值 |
|---|---|
| 每轮通过数 | 39 / 39 / 39 |
| pass rate / answer success / false refusal | 97.5% / 95.0% / 5.0%（3 轮完全相同） |
| 跨轮稳定性 | 100%（39 题 3/3 通过，1 题 0/3 失败，没有不稳定的 case） |

和 Env V1 baseline（`38afa41`）的结果完全一致；两次运行之间的 Agent 代码和索引指纹也完全相同。

### 13.3 DeepSeek 3 轮结果（`eval/stage25_deepseek_env_v1.json`）

| 指标 | 值 |
|---|---|
| 每轮通过数 | 39 / 39 / 39 |
| pass rate / answer success / false refusal | 97.5% / 95.0% / 5.0%（3 轮完全相同） |
| 跨轮稳定性 | 100%（39 题 3/3 通过，1 题 0/3 失败，没有不稳定的 case） |

### 13.4 诊断对照（现有的 Diagnostic Eval 对每一个 case-run 都做了诊断，下表按轮次汇总）

| | Qwen | DeepSeek |
|---|---|---|
| primary routing / planning / tool / retrieval / evidence / generation | 0 / **3** / 0 / 0 / 0 / 0 | 0 / **3** / 0 / 0 / 0 / 0 |
| secondary effects | 3（evidence_error） | 3（evidence_error） |
| latent issues | 0 | 0 |
| unattributed | 0 | 0 |
| 每一轮 | 各 1 个 planning_error primary，加 1 个 evidence_error secondary | 相同 |

诊断报告在 `eval/diagnostics/stage25_{qwen,deepseek}_env_v1.diagnostic.{json,md}`。

### 13.5 case 级转移（Qwen → DeepSeek，每边各 3 轮）

| 转移类型 | 数量 |
|---|---:|
| stable pass（两边都是 3/3） | 39 |
| fixed（0/3 → 3/3） | 0 |
| newly failed（3/3 → 0/3） | 0 |
| unchanged failure（两边都是 0/3） | 1：`answer_document_h008`（两边的行为都是 `policy_refuse`） |
| unstable（任意一边部分通过） | 0 |

### 13.6 Latency、token、调用次数和成本

| | Qwen（本地） | DeepSeek |
|---|---|---|
| 每个 task 的耗时（平均 / p95） | 3.69 / 10.48 秒 | 1.73 / 5.52 秒 |
| 每个 task 的 LLM 耗时（平均） | 3.20 秒 | 1.24 秒 |
| prompt / completion token 总数 | 52,211 / 4,386 | 57,764 / 3,200 |
| 每个 task 的 prompt / completion token | 435.1 / 36.5 | 481.4 / 26.7 |
| DeepSeek 缓存命中 / 未命中 token | — | 21,750 / 36,014（命中率 37.6%） |
| LLM 调用总数（每个 task） | 132（1.10） | 134（1.12） |
| tool 调用总数（每个 task） | 108（0.90） | 108（0.90） |
| API 成本合计 | $0（本地运行，硬件成本不计） | **$0.00739** |
| 每个 task 的成本 | $0 | **$0.0000616**（120 个 task-run） |
| 每个成功 task 的成本 | $0 | **$0.0000631**（117 个通过） |

- **成本的计算口径**：按列表价乘以记录下来的 usage（按缓存命中/未命中分别计价，按调用时间区分高峰和非高峰）。134 次调用全部落在非高峰时段；有 1 次预热调用不计入 task 成本。这个数字**没有和账单核对过**。
- **token 数只作描述**：两个模型用的是各自的 tokenizer，token 数不能拿来比较上下文大小，也不能说明任何上下文优化的效果。
- **latency 的差异**包含了本机硬件和网络两方面的因素（Qwen 在本机 GPU/CPU 上推理，DeepSeek 走网络）。它反映的是这台机器上这次运行的情况，不是两个模型的固有速度。

### 13.7 Prompt adaptation（必须如实说明）

- 两组的**逻辑 prompt** 完全相同，因为 Agent 代码和输入都相同。但 **DeepSeek 实际发送的 prompt 和 Qwen 并不是字节级相同的**：DeepSeek 不支持用 JSON Schema 约束输出，所以 schema 类请求会改为 `json_object`，并在 system 消息末尾追加一段 JSON Schema 说明。
- DeepSeek 组中，有 **105 次调用**的实际 prompt 与逻辑 prompt 不同，原因全部是 `schema_not_supported_by_json_object`（这 105 次就是回答生成和证据复查调用）。其余 29 次 rerank/select 调用的 prompt 本来就包含 "JSON"，所以没有被改动。
- Qwen 组的实际 prompt 与逻辑 prompt 不同的次数是 0。
- 每一次调用的逻辑 prompt hash、实际 prompt hash 和 adaptation 记录都保存在 Trace 的 llm_call span 里。

### 13.8 h008 的 Trace 对比（第 1 轮）

```
qwen      run 22e6ef4b…  completed  provider=ollama/qwen3:4b        llm_calls=0 tokens=0+0  25.6ms
deepseek  run 22397653…  completed  provider=deepseek/deepseek-flash llm_calls=0 tokens=0+0  14.6ms
  · [planner] plan_request ok
  · [planner] availability_check ok
  · [tool_call] execute_plan ok
      · [tool_call] document_search ok
  · [evidence] evaluate_evidence ok
  （两组都没有 generation span，也没有 llm_call span）
```

两组在每一步上都完全一致：route 是 `document_only`，**`requires_freshness=True`**；检索到 `chunk:1`（工作时间）、`chunk:17`、`chunk:4`、`chunk:7`；Evidence Policy 判定 `refuse` / `freshness_unsupported`；答案都是"根据现有资料无法确定。"；**两边都没有调用模型**。

这说明 h008 的失败**和模型无关**：它在调用任何 LLM 之前就已经被 Planner 的 signal 和 Evidence Policy 决定了，所以换模型不可能改变它。诊断结论（planning_error）在两组中完全相同。

### 13.9 实验结论

1. **在 eval-env-v1 / validation_v1 上，把 Qwen 换成 DeepSeek，官方结果没有任何变化**：6 轮全部是 39/40，40 个 case 里没有一个发生转移，诊断分布也完全相同。
2. **这个数据集没法区分这两个模型**：唯一的失败（h008）发生在模型被调用之前；其余 39 题两个模型都能稳定答对。所以可以说"在这 40 题上，两个模型在正确率上没有差异"，但**不能**据此得出"两个模型能力相当"。validation_v1 是已经被看过的回归集，模型差异更可能体现在更难、没被看过的数据上（按要求，本阶段不读 blind_v2）。
3. **能观察到的差异在效率上**：这台机器上 DeepSeek 的 task 耗时约为 Qwen 的 47%（p95 约 53%），代价是每个 task 约 $0.00006 的 API 成本。
4. **诊断链路对换模型是稳健的**：同一个失败在两种模型下被归到了同一个阶段、同一条规则，secondary 也相同，没有因为换模型而出现 unattributed 或 latent。
5. **需要改进的是 Planner 的时效 signal，而不是模型**（这是诊断给出的方向，本阶段不做任何修改）。

### 13.10 测试

新增 `tests/test_model_comparison.py`（3 个测试），覆盖高峰时段的判定（周末和窗口边界）、按缓存命中/未命中拆分并区分高峰计价（没有缓存拆分时成本记为未知，不做估算），以及 case 转移分类（部分通过的 case 单独记为 unstable，不会被塞进那四类）。

```
.\.venv\Scripts\python.exe -X utf8 -m unittest discover
Ran 765 tests in 25.951s
OK
```

### 13.11 风险和限制

- **R1 · 结论只适用于 validation_v1**：这个数据集已经被看过，而且已经到了"天花板"（39/40）。它不能说明两个模型在更难的问题上会怎样。
- **R2 · 实际发送的 prompt 不是字节级相同**（见 §13.7），比较的是"在同一个 Agent 下的两个 provider"，而不是"完全相同输入下的两个模型"。
- **R3 · 每组只跑了 3 轮**。两组都完全稳定，但 3 轮对于检测低概率的波动是不够的。
- **R4 · 成本是按列表价估算的**，没有和 DeepSeek 的账单核对；中国法定节假日这个非高峰例外没有建模（这次所有调用都在工作日的非高峰时段）。
- **R5 · eval_env 的 metadata 在 DeepSeek 组有两处缺口**（本阶段按要求没有修改 eval_env，记为 TD）：
  - `environment.chat_model` 是拿 `deepseek-flash` 去 Ollama 查的信息，结果全是空值，没有意义。实际的模型身份请看 `llm_provider_config`、Trace 里的 provider/model，以及 usage 日志里 DeepSeek 响应返回的 `model`。
  - `observed_llm_requests` 只统计了 Ollama 的 `/api/chat`，所以 DeepSeek 组显示 0 次 chat 调用。DeepSeek 的调用完整记录在 `eval/artifacts/stage25_deepseek_env_v1/llm_usage.jsonl` 里（135 次，全部带缓存拆分），以及 Trace 里（134 次 case 内调用）。
- **R6 · latency 取决于这台机器和当时的网络**，换一台机器数字会不一样。
- **TD6 · 可以在 eval_env 里按 provider 区分记录模型信息，并把监听范围扩展到 OpenAI 兼容接口**。这属于 eval_env 的改进，本阶段没有做。

按要求在这里停止：Stage 2、2.1 和 2.5 都没有修改 Agent 行为；没有根据 Stage 2.5 的结果去改 Agent。

## 14. Integration Milestone — 合入 origin/main@bac4d69（M10）

> 目标：把 main 上最新的 M10 可靠性工作合入 `stage0-llm-provider`。保留全部历史，不 rebase，不强推。不进入 Stage 3。结果标记为 **post-main-integration baseline**（series `pmi`）。Stage 0–2.5 的结果全部原样保留。

### 14.1 合并本身

- merge commit：`f784f3e`，`--no-ff`。父提交是 `a4ab04c`（本分支）和 `bac4d69`（origin/main）。之后的提交：`b8c8197`（runner 增加 series），`9f16680`（pmi 结果）。
- main 带进来的改动：68 个文件。包括 planner +425 行，rag.py +1631 行（答案校验、`decide_delivery`、缓冲流式输出、检索预算），adapters，以及 361 个新测试。eval-env-v1 的输入文件没有变。
- **唯一的文本冲突是 `rag.py`**。解决方式：
  - 业务逻辑以 main 为准，逐字采用。
  - 把 main 里的 6 处 LLM 调用重新接到 `llm_provider.get_provider()` 上（这样也就接上了 Trace）：
    - `rerank`
    - `select_for_subquestions`
    - `answer`
    - `_evidence_recheck`
    - `answer_structured`
    - `answer_stream`（改用 `chat_stream`，main 的缓冲逻辑和 `yield decide_delivery(...)` 保持不变）
  - `rag.py` 里已经没有直接的 `/api/chat` 调用。剩下的唯一一处 `requests.post` 是 embedding。
  - `CHAT_MODEL` 改为 `llm_provider.load_config().model`。
- **`LLMResponse.raw_content`（新增字段，只增不改）**：
  - 原因：provider 默认会剥掉 `<think>`，但 main 的 `extract_answer_text` 要先解析 JSON 外壳，再剥 think 标签。如果直接用 provider 剥过的 `content`，main 已经修好的 bug 会回来：答案正文里如果有字面的 `<think>`，会被弄坏。
  - 做法：rag.py 统一读 `raw_content`；Trace 在 `raw_content` 与 `content` 不同时，把它记到 `span.output["raw_content"]`。
  - 回归测试：`tests/test_llm_provider.py::test_rag_extraction_sees_the_raw_model_text`。
- `chat_orchestration.py` 是自动合并的：main 的 `INVENTORY_TERMS` 和 Stage 1 的 trace span 都在，已人工核对。
- 配套修改：
  - main 把阈值判断重构成了 `bm25_confident()`，导致 `agent_trace` 和 `eval_env/common.py` 记录的检索阈值变成 None。
  - 修复方式：两处都改为先读常量 `BM25_CONFIDENT_SCORE/RATIO`，读不到再退回到正则匹配源码。
  - 同时新增记录 `MAX_SUB_QUESTIONS`、`PADDING_SCORE_RATIO`，以及 wiki 的 `TITLE/ALIAS/SUMMARY/CLAIM_WEIGHT`。

### 14.2 Planner 行为差异（没有文本冲突，但做了审查）

- 方法：对比旧 planner（`db3653a`）和新 planner。只用见过的数据，即 dev 和 validation_v1 的 answerability 与 routes，去重后共 233 个问题。**没有读 holdout 或 blind 数据。** 产物是 `eval/artifacts/planner_diff.json`（gitignored）。
- 路由：新旧 planner 都是 233/233 与 `expected_route` 一致，没有任何路由变化。
- 计划变化共 8 条：
  - 7 条的 `requires_exact_citation` 从 False 变为 True：
    - refuse_missing_h001、refuse_missing_h006
    - route_document_only_005、route_document_only_009
    - answer_document_h001、answer_document_h002、answer_document_004
  - 1 条只有 reason_codes 变了。
- main 新增的规则：
  - `DOCUMENT_PRECISION_MARKERS`
  - `AUTHORITY_QUESTION_PATTERN`
  - `QUANTITY_INTERROGATIVE_PATTERN`
  - 社交性的结束语、感谢、祝愿识别
  - 否定和转述处理（"别"、转发、打发）
  - `STOCK_NOUNS`
  - 纯系统子句检测
  - `document_focus()`
  - `RECORD_ID_PATTERN`
- **h008 的计划没有变化**：`requires_freshness=True`。
- 除 planner 外还有一处行为变化：main 的 delivery validation（`decide_delivery`、答案校验）会改写最终交付的文本（见 14.5）。

### 14.3 测试与 gate

- 全量测试：合并后 1127 个通过。加入 runner 测试后是 1129 个（旧 765 + main 361 + 新增 3），全部通过。
- `python -m eval_env verify eval-env-v1`：18/18 通过。
- 路由评测：validation_v1 和 dev 的 overall 都是 1.0；dev 的 `exact_citation_signal_accuracy` 是 0.975。
- `evaluate_answerability --validate-only`：OK。

### 14.4 新 baseline（series `pmi`，post-main-integration baseline）

实验设置：

- 在干净提交 `b8c8197` 上运行（Agent 代码与 merge commit 完全相同）。
- 两轮运行都标为 reference 且 baseline_eligible。
- 两个 arm 的代码、retriever 配置和 index fingerprint 都相同，fingerprint 也与 Stage 2.5 相同。
- 产物：
  - `eval/post_main_integration/{experiment.json, qwen_vs_deepseek.json, .md}`
  - `eval/pmi_{qwen,deepseek}_env_v1.json`
  - `eval/diagnostics/pmi_{qwen,deepseek}_env_v1.diagnostic.{json,md}`

结果：

| | Qwen | DeepSeek |
|---|---|---|
| 每轮通过数 | 39/39/39（共 40 个 case） | 39/39/39 |
| answer success / false refusal | 95% / 5% | 同左 |
| 稳定性 | 100% | 100% |
| primary error | planning_error ×3（h008） | 同左 |
| secondary | evidence_error ×3 | 同左 |
| latent / unattributed | 0 / 0 | 0 / 0 |
| latency mean / p95 | 3.60 / 10.47 s | 1.96 / 5.89 s |
| tokens prompt / completion | 55219 / 3891 | 58837 / 3101 |
| DeepSeek cache hit / miss | – | 29169 / 29668 |
| LLM / tool 调用 | 132 / 108 | 132 / 108 |

- Qwen → DeepSeek 的 case 转移：stable_pass 39，unchanged_failure 1（h008），fixed、newly_failed、unstable 均为 0。
- DeepSeek 成本：共 $0.012797，每个任务 $0.00010664，每个成功任务 $0.00010937。
  - **132 次调用全部落在高峰时段（价格 ×2）**，所以不能直接和 Stage 2.5 的 $0.00739（非高峰）比较。
- DeepSeek 的 prompt adaptation 与 Stage 2.5 相同（json_object 加 schema 说明），已写入报告。

### 14.5 与 Stage 2.5 的对比（同一环境、同一模型，只有代码变了）

- 两个模型的通过数和行为（answer / refuse）都没有变化。
- 答案文本有变化：Qwen 11 个 case，DeepSeek 14 个 case。
- 这些变化都来自 main 的 delivery validation：
  - Qwen refuse_missing_h006：以前是一段冗长的推理加复述，现在是干净的"根据现有资料无法确定"。
  - DeepSeek 的拒答统一规范成"根据现有资料无法确定。"。
  - Qwen answer_document_h003：答案变短了。
- 多个 case 的不同答案数量减少了，例如 2 种变为 1 种，说明交付文本更稳定。

### 14.6 h008

- h008 在两个 arm、两个 series 中完全一致：
  - 路由 document_only，`requires_freshness=True`。
  - 检索到 chunk:1（工作时间）。
  - evidence 阶段因 `freshness_unsupported` 拒答，没有发生 LLM 调用。
- 结论：这个失败与模型无关，M10 的 planner 也没有改变它。根因仍是 planner 把"目前的制度里"判为需要时效性，而语料无法提供时效证据。
- 修复属于 Agent 行为改动，按要求本阶段不做。

### 14.7 能否合入 main

- `origin/main@bac4d69` 是 HEAD 的祖先，main 可以 fast-forward 到本分支，新增 21 个 commit。前提是合入时 origin/main 没有再前进。
- 测试、gate 和 baseline 都达标。
- 合入前需要知道的问题：
  1. README 没有任何 Stage 0–2.5 的内容（LLMProvider、DeepSeek、Trace 都提到 0 次）。
  2. `code_sha256` 按工作区文件计算哈希。切换分支后工作区变成 CRLF，`eval_env/environment.py` 因此被误判为改过，但 git blob 其实相同。应该改为对规范化后的 blob 内容计算哈希。
  3. main 的 M10 把 blind_v2 的路由结果当作验收基线（"V2 路由整体正确率 57/80"），所以在 main 上 blind_v2 已经不再是盲测集。
  4. `eval/runs` 和 console 文件里还有本机绝对路径，而仓库是公开的。
  5. DeepSeek 在高峰和非高峰的价格不同，成本比较时必须看调用时段。
  6. eval_env 的 `chat_model` 和 `observed_llm_requests` 对 DeepSeek 不准确（TD6）。
- 合入后的 `.gitignore` 包含 `.env`、`.env.*`、`!.env.example` 和 `eval/artifacts/`，补上了 main 缺少的 `.env` 规则。

按要求在这里停止：没有 push main，没有打 tag，没有进入 Stage 3。本阶段没有根据结果修改 Agent。

### 14.8 发布前收尾：README 与 Trace 开销重测

- **README 重写**（`87c8bea`）：
  - 第一屏说明项目定位、主链路、已验证的结果，并提示 blind_v2 已被开发使用。
  - 旧 README 的应用层内容原文移到 `docs/APP_DETAILS.md`，包括演示、API、Wiki、M10 验收和完整目录树，README 里有链接。
- **Trace 开销重测**：
  - 在当前集成版本 `551bab2` 上重测，工作区干净。结果在 `eval/pmi_trace_overhead.json`（`7532f2f`）。
  - 测量脚本加了 `--output` 参数，不会再覆盖 `eval/stage1_trace_overhead.json`。
  - 结果：

| 接口 | OFF p50 / p95 | ON p50 / p95 | 平均差值（95% CI） | 占真实请求 p50（Qwen 2.81 s / DeepSeek 1.16 s） |
|---|---|---|---|---|
| `/api/chat` | 23.3 / 47.3 ms | 42.4 / 66.8 ms | +20.9 ms [+18.5, +23.2] | 0.74% / 1.8% |
| `/api/chat/stream` | 28.3 / 46.4 ms | 50.3 / 75.5 ms | +23.9 ms [+21.9, +25.8] | 0.85% / 2.1% |

- **与 Stage 1 对比**：
  - `/api/chat` 基本没变（+19.4 → +20.9 ms，两个置信区间重叠）。
  - 流式接口从 +19.6 ms 增加到 +23.9 ms，置信区间不重叠。可能与 main 的缓冲流式输出有关，但没有深挖。
  - README 的第一屏不再展示开销；当前数字放在 Trace 小节和 Current metrics 里，Stage 1 的 +0.7% 标为历史测量。

## 15. Stage 3 — Freshness planning（h008 这一类问题）

> 目标：修复 Trace 和 Diagnostic Eval 定位出来的 freshness 规划错误，也就是 h008 这一类问题。
>
> 分支 `stage3-freshness-planning`，从 `main@224f2e7`（PR #5 合入后）建立。
>
> 顺序：先提交标签，再跑 baseline，然后准备隔离的 holdout，再实现 A′、跑 dev，最后一次性打开 holdout、跑 validation 回归。
>
> 本阶段不进入 Tool Use Stage。

### 15.1 问题的根因

- **Planner：** `_clause_signals` 只要子句里出现 `FRESHNESS_MARKERS` 中的词，就判为 freshness。这是纯子串匹配，不看这个词修饰的是什么。
- **Evidence Policy：** freshness 只能由带 `version` 或 `observed_at` 的文档证据，或者带 `observed_at` 的系统证据来满足。但 `document_adapter` 固定写 `version=None`、`observed_at=None`，Wiki 的页面版本又被明确排除在外。
- **结果：** 任何被标了 freshness、计划里又没有系统步骤的请求，都必然以 `freshness_unsupported` 拒答，而且不调用模型。
- **这不是个例：** 除了 validation_v1 的 h008，dev answerability 集里的 `answer_document_008` 从 8 月起就以同样的方式失败。

### 15.2 专项开发集与 baseline（Planner 未改动）

- **标签：** `eval_temporal_freshness_dev.json`，共 29 条：当前有效知识 12 条、实时业务状态 9 条、混合 3 条、时间词另有含义 5 条。
  - 在 `e74ee62` 提交，早于任何 Planner 修改。写标签时没有运行 Planner。
  - 标注规则见 `eval/stage3/LABELING.md`。`48fa696` 只修正了覆盖范围的措辞，标签没有变。
- **baseline：** 评分脚本在 `9751c10`，结果在 `cb5bdae`，工作区干净。

| 指标 | 值 |
|---|---|
| freshness accuracy | 0.483（14/29） |
| TP / FP / FN / TN | 10 / 13 / 2 / 4 |
| 应该能回答、却会被拒答 | 13/17（用真实的 `evaluate_evidence` 逐条核实） |

- **错误模式：**
  - **P1：** 只要出现时间词就判 freshness，不看它修饰什么。共 13 个误报，全部会导致拒答。
  - **P2：** 词表缺"今天 / 最近"，造成 2 个漏报。
  - **P3：** 有 3 条判对了，但原因是那几个词恰好不在词表里，所以单纯扩充词表会让结果变差。
  - **P4：** "我的年假还剩几天"被路由成了 document_only。

  详见 `eval/stage3/BASELINE_REVIEW.md`。

### 15.3 隔离的 holdout

- **作者：** 由一个全新上下文的子 agent 编写，共 24 条，在 `7cc5abe` 提交，sha256 `b24d326e…`。
  - 它只能读三样东西：`LABELING.md` 的语义部分、`sample_company_rules.md` 和业务 fixture。
  - 它不能读 Planner、dev 集和候选方案，也没有运行 Planner。
  - 它的报告里只有条数和分布。在打开之前，实现者没有读过内容。
- **开封规程先于实现提交：** `eval/stage3_freshness_holdout.py` 在 `d53ab34` 提交，早于 A′ 的实现。
  - 它只能运行一次。运行前要校验 sha256、要求工作区干净、要求文件在封存之后没有被改过。
  - 它在同一批 case 上同时给 `main` 的旧 Planner 和新 Planner 打分。

### 15.4 A′ 的实现（`307a12b`）

- **新规则：** `_clause_requires_freshness`。时间词只有出现在实时状态子句里，才判为 freshness。
  - 实时状态子句指 `needs_system` 为真的子句，或者"第一人称 + 还剩 / 剩余 / 余额 / 还有多少"这类个人余额。
- **新增词表：** `LIVE_TIME_MARKERS`（今天、今日、最近、近期、这几天）。这些词只在实时状态子句里计入 freshness，永远不会触发系统查询。
- **不改的部分：** 路由逻辑和 `weak_state_intent` 都没有动。
- **测试改动：**
  - Planner 有 2 条旧测试写的是旧语义（"最新公告是什么"、"现在的订单管理制度怎么规定"都断言为 True），已改为 False 并注明原因。
  - 新增 7 条 `FreshnessScopeTests`，其中包括一条与 Evidence Policy 的联动检查。
  - Stage 2 有 2 条诊断测试原来拿真实的 h008 bug 当夹具。现在改为用 patch 精确还原 main 的旧规则来复现误判，这样诊断归因能力仍然被测到。
  - 新增 1 条对照测试：用新 Planner 跑 h008，每个阶段都通过。

### 15.5 dev、holdout 与 validation 结果

| | 修改前 | A′ |
|---|---|---|
| dev freshness（29 条，`1aaffb7`） | 0.483 | **1.000**；路由 27/29，没有变化；误拒 13/17 → 0/17 |
| holdout freshness（24 条，只开封一次，`c786cf4`） | 0.625；FP 5 / FN 4 | **0.792**；FP **0** / FN 5；precision 0.545 → 1.000；recall 0.600 → 0.500；路由 0.833，没有变化 |
| 全量测试 | 1129 | **1140**（新增 8 条 Planner 和诊断测试，3 条 label revision 测试） |
| 路由评测 validation_v1 / dev（原标签） | 路由 1.0 / 1.0；freshness 80/80、80/80 | 路由 1.0 / 1.0；freshness **79/80、79/80**（只差冲突的 2 条） |
| 同上，应用 label revision overlay | – | freshness 80/80、80/80 |
| Qwen eval-env-v1 × 3 轮（`5f0ff42`） | 39/40 × 3（pmi） | **40/40 × 3**；7 项 gate 全部通过；误拒率 0% |
| Diagnostic Eval | h008：planning_error ×3 | 主错误、连带影响、潜在问题和 unattributed 全部为 0 |

- **h008：** 从 0/3 变成 3/3，答案是"核心协作时间为上午 10:00 至 12:00、下午 14:00 至 17:00。[来源 1]"。其余 39 个 case 的通过情况和行为都没有变化。
- **dev 上的 1.000 有拟合成分：** 这些标签是看过失败模式之后写的，规则也是对着它设计的。泛化能力以 holdout 为准：误报 5 → 0 说明 A′ 针对的问题确实泛化了，但 recall 没有提高。

### 15.6 冻结标签的修订（overlay，不修改原数据集）

- **修订文件：** `eval/label_revisions/stage3_freshness_contract.revisions.json`，修订了 2 条：
  - dev 的 `route_document_only_004`（"现在这版考勤制度……"）
  - validation_v1 的 `route_document_only_h005`（"目前生效的这版保密制度……"）
  - 两条都是 `expected_requires_freshness` 从 True 改为 False，理由写在文件里。
- **哈希：** 同时记录评测脚本看到的哈希（工作区 CRLF）和 LF 规范化后的哈希。后者在不同检出之间是稳定的。
- **重新打分：** `eval/apply_label_revisions.py` 把原标签分数和修订后分数并列报告，结果在 `eval/stage3/route_eval_with_revisions.json`。这两条永远不算作 Planner 的改进。
- **测试：** `tests/test_label_revisions.py` 校验修订文件：数据集的 LF 哈希必须一致，旧值必须与原数据集一致，并且不允许指向 blind 或 holdout 数据集。

### 15.7 语义契约

- **定义：** 当且仅当请求要求一个实时业务状态的值，并且用时间表达把它限定在当前时点时，`requires_freshness` 才是 True。
  - 制度内容不算，即使前面有"目前 / 现行 / 最新"。
  - 只有实时状态请求、没有时间表达的，也不算。这与冻结数据集的标注一致；在运行时也没有区别，因为系统证据总带 `observed_at`。
- **完整定义**见 `eval/stage3/LABELING.md` 的「`requires_freshness` 语义契约」。
- **holdout 的偏差：** tfh_018 没有时间表达却被标为 True，按契约应该是 False。holdout 已经开封，按原标签如实记录，不重新打分。

### 15.8 已知漏报（均已接受，不再继续修）

| holdout case | 原因 | 运行时影响 |
|---|---|---|
| tfh_014 "截止到现在，我的调休余额还有多少小时？" | 逗号把时间词切成了单独一个子句（A′ 新引入） | 无。路由是 system_only，系统证据带 `observed_at` |
| tfh_007 "按目前的年假规定……系统里我今年的年假还剩几天" | 时间词在制度子句里，实时子句只有"今年"（A′ 新引入） | 无。计划里有系统步骤 |
| tfh_018 "sku-a100 还有货吗？够不够发 50 件？" | 没有时间表达（main 也漏）；按契约应为 False | 无 |
| tfh_008 "订单 ord-1002 我刚刚付过款了，它的状态更新了没有？" | "刚刚"不在词表里，路由也错了（main 也漏） | 问题在路由，不在 freshness |
| tfh_021 "我本周提交的远程办公申请，主管批了没有？" | "本周"不在词表里，路由也错了（main 也漏） | 问题在路由，不在 freshness |

另外还有两条漏报，但 A′ 故意保留：dev 的 tfh_ls_05 和 tfh_mx_01（"我的年假还剩几天"）的路由缺口没有修。A′ 让它们继续判为 freshness，从而安全地拒答，而不是用制度条文回答个人余额问题。

### 15.9 为什么不继续做 A″

A″ 的思路是：时间词和实时请求在同一个请求的不同子句里也算 freshness，同时补充"刚刚、本周、今年"一类词。不做的理由：

1. **没有运行时收益。** `requires_freshness` 在运行时只有 Evidence Policy 在用。A″ 能修的只有路由正确、有系统步骤的漏报（tfh_014、tfh_007），而这类请求的系统证据一定带 `observed_at`，freshness 检查本来就会通过。所以 A″ 不会改变任何一个回答。
2. **没有干净的衡量手段。** holdout 已经开封，任何继续调整都只能在见过的数据上评估，得出的数字说明不了泛化能力。按要求本阶段不重新生成 holdout。
3. **真正影响回答的问题在路由，不在 freshness。** 例如逗号把记录号和状态词拆开、"申请"没被识别为系统对象、个人年假余额没被识别为系统请求。这些都会改变路由，超出 Stage 3 的范围。
4. **风险不对称。** 误报会直接造成误拒答，漏报在当前架构下没有影响。A′ 以 precision 1.000 为代价换来了较低的 recall，这符合这个信号在运行时的实际作用。

### 15.10 已知限制与后续

- 以后如果要继续改进 freshness，必须先在 dev 上开发，再用一份**新的**隔离 holdout 来衡量。
- 路由缺口需要单独立项。
- README 的 Current metrics 里写的还是 post-main-integration 的数字（39/40 × 3、h008 未修复），本阶段没有改 README。
- 本阶段没有重跑 DeepSeek 对照。A′ 只改变 freshness 标记，不改变路由和生成，而且 h008 的失败本来就与模型无关。

按要求在这里停止：Planner 在 A′ 之后没有再修改；没有重新生成 holdout；没有进入 Tool Use Stage。


## 16. GroundedAgent V2 Stage 4.3：售后 Policy + Wiki Lifecycle

- 基线 `bc05c693bc89c1ace67415710141006fea1b8ab2`；工作分支 `stage4-policy-wiki`。实现验收后仅交付 commit / push / PR，禁止 merge。
- 正式规则仅从严格 JSON front matter 解析；复用 WikiRepository 文档快照、draft/diff/publish/rollback；runtime catalog 只读当前发布 build。业务优先级和 `[effective_from, effective_to)` 时间窗分别独立于 Evidence authority 和 publication wall clock。
- `search_after_sales_policy` 默认绑定真实 adapter，默认 `PublishedPolicyCatalog()` 只读 committed 冻结包 `wiki_pages/aftersales_frozen/`（clean checkout 即可用；live root 可显式注入）；5 个 runtime tools 全部只读。`validate_policy_refs(refs, snapshot=CatalogSnapshot, evidence=...)` 以 snapshot 为来源锚点，校验 build/version/source_version/source_digest/provenance 等全部字段。
- 冻结包 manifest `docs/v2/stage4.3-frozen-manifest.json`。`compile_policy_draft()` 始终产生 DRAFT；通用 `WikiRuntime()` 默认仍 upload → publish，显式 `hold_as_draft=True` 才保留草稿。旧聊天来源映射测试显式注入原企业 Wiki fixture，NotReady 错误测试显式注入失败 adapter。完整逐文件记录见本阶段交付报告。
- 最终（含 PR #10 review 修复）1525 tests passed（1437 原有 + 88 Stage 4.3），0 skip；Wiki 255/255，V1 evidence_policy 71/71，11/11 semantic mutants killed。结果与源码 hash：`docs/v2/stage4.3-validation.json`。
- 真实临时仓库演示：15 天 → 新 draft 不可见 → publish 20 天 → rollback 15 天；ORD-1001 在 2026-11-15 促销窗口内、2026-12-01 标准窗口外，两次真实 policy_refs/Evidence 校验通过，业务 DB 未写。
- TD: Wiki compiler still uses current compiler path; formal Stage 4 eval consumes a frozen build. 默认售后构建复用确定性 verbatim assembler；没有做真实在线模型编译验收。
- category evidence 与 delivered_at evidence 的同订单结构化关联：**已由 PR #13（merge `d9b6ab0`）解决**。`BusinessEvidence.relations` 由数据源产出 `order_id`（售后单另有 `order_item_id`），窗口派生在缺失 / 不一致时分别 fail closed 为 `order_link_missing` / `order_link_mismatch`，不解析 locator / content。
- 多包裹歧义（Stage 4.3.8）：schema 没有 order_item → tracking_no 映射，同订单不等于同包裹。明细级窗口必须经 `derive_item_window_eligibility`（传入该订单一次观测的全部 delivered_at）：1 个包裹才计算，多个包裹为 `item_package_link_ambiguous`，不任选包裹；Stage 4.4 / 5 不得绕过该入口直接用低层 `derive_window_eligibility` 做明细级判断。
- 完整报告、schema、CLI、diff、冻结方案、fixture 变更与边界：`docs/v2/stage4.3-handoff.md`。
- 未开始 Stage 4.4、Planner/Router、Eval/数据集/holdout、Tool Loop、主聊天 API 切 V2、frontend、业务写动作、Guard 或 approval。
