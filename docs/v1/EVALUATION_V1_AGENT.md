# V1 三通道 Agent 评测报告

评测日期：2026-08-27（M7）／2026-08-28（M7.1 盲测重建）

本报告评测 **V1 orchestrated 三通道链路**（Planner → Executor → Evidence Policy →
回答生成），不是旧版 legacy 单步检索链路。旧版结果保留在 `EVALUATION.md`，两份
报告的数据集、链路和口径都不同，**不可互相替换或合并**。

本轮只做评测，不修改任何生产代码。发现的缺陷记录在第 10 节，不在本轮修复。

> **一句话结论**：在一份由独立 Agent 盲写、经相似度门禁验证的
> **Blind Holdout V1（pre-fix audit）** 上，Planner 路由准确率为 **22.5%**，
> 回答/拒答通过率为 **54.2%**。此前 95.0% / 97.5% 的数字来自与开发集模板高度
> 相似的数据，**不能代表泛化能力**。

> **⚠ Blind Holdout V1 的现状（M8A 起生效）**
>
> 该集合已经完成计分，并已用于逐题缺陷分析（见第 7、10 节）。
> **从现在开始它是回归审计集（regression audit set）**，
> **不能再作为修复后的未见盲测集，也不能作为最终简历指标。**
> 修复后在该集合上重跑得到的任何数字，只能表述为
> *post-fix diagnostic on an already-seen audit set*。
> 真正未见的 **Blind Holdout V2** 将在 M8A 代码冻结后由另一个全新 Agent 编写。

---

## 1. 环境

| 项 | 值 |
|---|---|
| Git SHA | `cf96fa2bfcb8b2ceffba169e226dcf3cabf65f05`（`origin/main`） |
| 分支 | `codex/v1-evaluation-expansion` |
| Python | 3.12.13 |
| 操作系统 | Windows-11-10.0.26200-SP0 |
| Chat 模型 | `qwen3:4b`（本地 Ollama） |
| Embedding 模型 | `nomic-embed-text`（本地 Ollama） |
| 文档语料 | `sample_company_rules.md`，20 个知识块 |
| Wiki 语料 | `wiki_pages/sample_company_wiki.json`，4 个页面 |
| System fixture | `system_fixtures/sample_business_system.sql`，内存库，每次请求新建 |

System 通道通过 `chat_orchestration.demo_system_connection()` 使用一次性内存
SQLite，**不读生产库，也不写任何业务状态**；评测不调用 `commit_exchange`。

---

## 2. 三类结果必须分开陈述

| 类别 | 对象 | 结果 | 说明 |
|---|---|---|---|
| **工程测试** | 397 项 unittest / API 回归 | 397/397 通过 | 代码回归，不是模型质量 |
| **路由质量评测** | Planner，确定性 | Dev 95.0% / Validation V1 95.0% / **Blind Holdout 22.5%** | 只评 route 与 steps |
| **回答/拒答质量评测** | 全链路 + qwen3:4b | Dev 97.5% / Validation V1 97.5% / **Blind Holdout 54.2%** | 可回答性、拒答、引用、来源 |

397 项工程测试**不构成**准确率证据。**在引用本项目质量数字时，应引用 Blind
Holdout 的结果**；Dev 与 Validation V1 只能作为内部回归基线。

---

## 3. 三套数据集，以及为什么 Validation V1 不再算 Holdout

| 数据集 | 条数 | 作者 | 定位 |
|---|---:|---|---|
| `eval_*_dev.json` | 80 / 40 | 主 Agent（已读 planner 实现） | 开发集 |
| `eval_*_validation_v1.json` | 80 / 40 | 同一主 Agent | **内部回归基线，不是 Holdout** |
| `eval_*_holdout.json` | 80 / 40 | 独立盲写 Agent | **唯一可对外引用的 Holdout** |

### 3.1 为什么 Validation V1 被降级

原先命名为 holdout 的两份数据存在三个问题，任一都足以使它失去盲测资格：

1. **作者不独立。** 它与 Dev 集由同一个 Agent 编写，而该 Agent 在编写前已完整
   阅读 `orchestration/planner.py`，包括全部 marker 常量表。题目因此天然贴合
   实现的词表，测的是「作者是否记得 marker」而非「系统是否理解自然语言」。
2. **与 Dev 集模板重复。** 用第 4 节的相似度工具实测，Route 集有 2 条 ≥0.85、
   7 条 ≥0.80（8.8%，超过 5% 上限），并有 6 条属于「只替换业务对象」的模板题；
   Answerability 集有 5 条 ≥0.80（12.5%）、10 条模板题。典型例子：
   `先大致介绍下物流这块儿…我的物流到哪一步了` 与
   `先大致介绍下订单这块儿…我的订单到哪一步了` —— 实体遮蔽后相似度 **1.000**。
3. **已被开发者与 Reviewer 查看。** 一旦看过失败案例，后续任何修改都可能无意
   识地迎合实现。

原结果**未删除、未修改**，逐题内容原样保留，仅文件名与结果目录改为
`validation_v1`。结果 JSON 内部的 `dataset` 字段仍记录运行时的旧文件名，这是
运行事实，不做追溯性改写。

### 3.2 Blind Holdout 的编写隔离

新 Holdout 由一个独立子 Agent 编写。它**实际读取的文件只有三个**：

- `sample_company_rules.md`
- `wiki_pages/sample_company_wiki.json`
- `system_fixtures/sample_business_system.sql`

它未打开 planner、评测器、测试、Dev/Validation 数据、`evaluation_runs/**` 或
任何文档。路由定义、产品能力边界与 JSON schema 以**枚举与语义描述**的形式直接
写在任务提示中，不含任何 marker 常量、判定逻辑、失败案例、准确率或混淆矩阵。
相似度门禁未过时，退回的 8 条只告知 ID 与「过短、缺少语境」的原因，
**未展示任何参考集原文**，以保持盲性。

### 3.3 数据集分布

Route（每份 80 条，8 类各 10 条）：

| 文件 | boundary | freshness=True | citation=True | 平均问句长度 |
|---|---:|---:|---:|---:|
| dev | 28 | 27 | 32 | 19.9 字 |
| validation_v1 | 29 | 27 | 34 | 19.2 字 |
| **holdout（盲写）** | **28** | **38** | **38** | **29.6 字** |

盲写集平均句长约为前两者的 1.5 倍，反映的是自然消息带语境、带缘由的写法，
而非刻意加长。

Answerability（每份 40 条，分布相同）：answer 20（document 8 / wiki 4 /
system 4 / multi_channel 4）、generation_refuse 8、policy_refuse 4、boundary 8。

### 3.4 SHA-256

```
7548b7ac90a66fe64b75de011db22e7e88492a00bb76dbeda7772d80765ab84c  eval_orchestrated_routes_dev.json
71435f6f22d3aa7b27ff47e39ff7518ff40b8361ea317e2e01f8e475f0a08051  eval_orchestrated_routes_validation_v1.json
35995dedacb5eefec5cd7333f85015e7599da581fa1f5ede81caccd9b421135f  eval_orchestrated_routes_holdout.json
a50596cd934c9b05ef65c198e6b8550d1f8d8447dedfd322f4f1a5783541cbac  eval_answerability_dev.json
0be35413352610d19008ca7de10c88c57982f9ee7d2731b77e1357aa9734fc07  eval_answerability_validation_v1.json
5f55f703502a703b6453bb6e08e42ebc70015b9f06f03b98d17163dd7a64a104  eval_answerability_holdout.json
```

Validation V1 的两个哈希与 M7 首次计分前记录的一致（重命名不改内容）。
Blind Holdout 的两个哈希在**首次计分运行之前**记录，运行后未再修改。

---

## 4. 跨集合相似度检查

`check_evaluation_overlap.py` 用三种视角比较 Blind Holdout 与 Dev、Validation V1
的每一条问题：规范化字符 SequenceMatcher、字符 bigram Jaccard，以及**实体遮蔽
签名**（把 SKU、数字、语料章节名与业务对象替换为占位符后再比对）。第三种是关键：
只替换实体的模板题，原始相似度可能因长实体名而降到阈值以下，遮蔽后却仍是 1.000。

冻结门禁：≥0.85 必须为 0；≥0.80 不超过各集合 5%；实体替换模板题为 0。

| 对比 | max | ≥0.85 | ≥0.80 | 实体替换疑似 | 结果 |
|---|---:|---:|---:|---:|---|
| Dev ↔ Validation V1（Route，参考） | 0.875 | 2 | 7 / 80 | 6 | 若按新门禁则 **不达标** |
| Dev ↔ Validation V1（Answerability，参考） | 0.846 | 0 | 5 / 40 | 10 | 若按新门禁则 **不达标** |
| **Blind Holdout（Route，首轮）** | 1.000 | 2 | 4 | 0 | **不达标 → 退回重写** |
| **Blind Holdout（Answerability，首轮）** | 0.867 | 3 | 4 | 0 | **不达标 → 退回重写** |
| **Blind Holdout（Route，重写后）** | 0.60 | **0** | **0** | **0** | **PASS** |
| **Blind Holdout（Answerability，重写后）** | 0.60 | **0** | **0** | **0** | **PASS** |

首轮退回的 8 条全部是**极短的裸查询**（`帮我看下 sku-c300 的库存`）加一条叠词
问候（`哈喽哈喽`，与 Validation V1 完全相同）。原因不是抄袭，而是**句子太短时
表达空间本身很小**，两个独立作者必然收敛。重写方式是给消息一个真实的存在理由
（盘点表填不完、客户三点来提货、采购催了三遍），**信息需求本身不变**，标签字段
逐一比对确认无漂移。这也是一条方法论结论：**极短问句不适合用作盲测独立性的载体。**

Blind Holdout 内部亦无任何遮蔽签名重复超过 2 次。

---

## 5. Route 评测结果

评测器直接调用 `plan_request`，不触碰 Ollama、网络或数据库，结果完全确定，
因此每份数据集只跑一次。PASS = route 与 steps 同时正确。

| 指标 | Dev | Validation V1 | **Blind Holdout** |
|---|---:|---:|---:|
| Overall Accuracy | 95.0% | 95.0% | **22.5%**（18/80） |
| Macro Accuracy | 95.0% | 95.0% | **22.5%** |
| Steps Exact Match | 95.0% | 95.0% | 22.5% |
| Freshness Signal Accuracy | 100.0% | 100.0% | 97.5% |
| Exact Citation Signal Accuracy | 100.0% | 98.75% | 70.0% |
| Boundary Accuracy | 85.7% | 86.2% | **35.7%**（10/28） |

### 5.1 每类 route 准确率

| route | Dev | Validation V1 | **Blind Holdout** |
|---|---:|---:|---:|
| `direct` | 80.0% | 80.0% | **0.0%**（0/10） |
| `wiki_only` | 100.0% | 100.0% | **10.0%**（1/10） |
| `document_only` | 100.0% | 100.0% | 100.0%（10/10） |
| `system_only` | 100.0% | 100.0% | **50.0%**（5/10） |
| `wiki_document` | 100.0% | 100.0% | **0.0%**（0/10） |
| `wiki_system` | 100.0% | 100.0% | **10.0%**（1/10） |
| `document_system` | 80.0% | 80.0% | **10.0%**（1/10） |
| `wiki_document_system` | 100.0% | 100.0% | **0.0%**（0/10） |

`document_only` 的 100% **不是能力**。它是 Planner 在没有任何信号命中时的兜底
路径，所有识别失败的题都落到这里。把它读成「文档路由很准」是错误的。

### 5.2 Blind Holdout 混淆矩阵

行 = 人工预期，列 = Planner 实际：

| 预期 \ 实际 | direct | wiki_only | document_only | system_only | wiki_document | wiki_system | document_system | wiki_doc_sys |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `direct` | **0** | 0 | 10 | 0 | 0 | 0 | 0 | 0 |
| `wiki_only` | 0 | **1** | 9 | 0 | 0 | 0 | 0 | 0 |
| `document_only` | 0 | 0 | **10** | 0 | 0 | 0 | 0 | 0 |
| `system_only` | 0 | 0 | 5 | **5** | 0 | 0 | 0 | 0 |
| `wiki_document` | 0 | 2 | 8 | 0 | **0** | 0 | 0 | 0 |
| `wiki_system` | 0 | 0 | 6 | 2 | 0 | **1** | 1 | 0 |
| `document_system` | 0 | 0 | 8 | 1 | 0 | 0 | **1** | 0 |
| `wiki_document_system` | 0 | 0 | 6 | 1 | 1 | 0 | 2 | **0** |

实际输出分布：`document_only` **62**、`system_only` 9、`document_system` 4、
`wiki_only` 3、`wiki_system` 1、`wiki_document` 1、`direct` 0、`wiki_document_system` 0。

**80 条里有 62 条（77.5%）塌陷到 `document_only`。** 面对自然语言，Planner
基本只有一种行为：回退到文档检索。三通道设计在盲测问法下没有被触发。

对照：Dev 与 Validation V1 的混淆矩阵完全一致且仅有 4 处错误。**同一代码在不同
构造方式的数据集上表现差异显著，说明原 Dev/Validation 数据存在明显构造偏差，
不能将差距归因于单一因素。** 作者是否读过实现只是其中一个可能因素；句长、
表达自然度、题材分布等同样不同，本轮设计无法把它们分离开来。

---

## 6. Answerability 评测结果

评测器走真实链路：`chat_orchestration.prepare()` → （需要生成时）
`rag.answer_structured()`，与 `api.py` 的 `orchestrated_chat` 同一条路径。
Planner、检索、Evidence、Policy、回答模型均未 mock。运行前预热一次，不计入结果。

`answer` 类要同时满足四项才算 PASS：全部事实组命中、`required_source_types`
全部出现、含合法 `[来源 N]`、引用编号落在实际 sources 范围内。

### 6.1 事实判定已收紧

Blind Holdout 改用 `expected_fact_patterns`（正则），Dev 与 Validation V1 保留
原有 `expected_fact_groups`（子串）以便原样重放。评测器同时支持两者，
**旧数据未被改写以适配新 schema**。

新 schema 强制：每个数字必须绑定单位或语义（`(?:8|八)\s*天`、`(?:7|七)\s*箱`、
`(?:2000|两千)\s*元`），**裸单字符数字模式在加载时即被拒绝**。正则在剥离
`[来源 N]` 标记与 SKU 串之后的正文上匹配，避免 `SKU-B200` 里的 `0`、`2` 或引用
编号被误判为事实命中。旧的子串写法（如 `["2","两"]`）被显式豁免并标注为已知弱点。

### 6.2 三轮结果

| 指标 | Dev(1轮) | Validation V1(3轮均值) | **Blind Run 1** | **Run 2** | **Run 3** | **均值** |
|---|---:|---:|---:|---:|---:|---:|
| Pass rate | 97.5% | 97.5% | 52.5% | 55.0% | 55.0% | **54.2%** |
| Answer Success Rate | 95.0% | 95.0% | 45.0% | 45.0% | 45.0% | **45.0%** |
| False Refusal Rate | 5.0% | 5.0% | 20.0% | 20.0% | 20.0% | **20.0%** |
| Unanswerable Refusal Rate | 100.0% | 100.0% | 83.3% | 91.7% | 91.7% | **88.9%** |
| Refusal Mechanism Match | 100.0% | 100.0% | 58.3% | 66.7% | 66.7% | **63.9%** |
| Boundary Message Accuracy | 100.0% | 100.0% | 62.5% | 62.5% | 62.5% | **62.5%** |
| Citation Presence Rate | 100.0% | 100.0% | 100.0% | 100.0% | 94.1% | **98.0%** |
| Citation Index Validity | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | **100.0%** |
| Required Source Coverage | 100.0% | 100.0% | 50.0% | 50.0% | 50.0% | **50.0%** |
| Expected Fact Hit Rate | 95.0% | 95.0% | 75.0% | 75.0% | 75.0% | **75.0%** |
| Expected Fact Group Hit Rate | 95.7% | 91.7% | 82.0% | 82.0% | 80.3% | **81.4%** |
| Route Accuracy | 100.0% | 100.0% | 57.5% | 57.5% | 57.5% | **57.5%** |

耗时（Blind Holdout）：

| Run | P50 | P95 | 最大 |
|---|---:|---:|---:|
| 1 | 7.01s | 13.29s | 16.21s |
| 2 | 6.99s | 11.87s | 15.13s |
| 3 | 7.85s | 12.21s | 15.26s |
| 合计 | 7.85s | 13.10s | 16.21s |

按预期行为拆分（Run 1 中位数）：`answer` 5.52s、`generation_refuse` 11.86s、
`policy_refuse` **11.53s**、`boundary` 0.00s（中位）但最大 11.95s。

`policy_refuse` 与部分 `boundary` 的耗时从 Validation V1 的 **0.00s 升到约
11.5s**，是一项可量化的退化：路由塌陷后这些请求不再走固定短路径，而是真的调用
了回答模型。拒答比回答慢约一倍以上，因为 `rag.answer_structured` 在首轮判定
拒答时会追加一次证据复查调用。

### 6.3 稳定性（按要求口径分列）

- **Stable pass（3/3 均通过）：21 条**
- **Unstable（1/3 或 2/3 通过）：1 条** —— `answer_holdout_026`
- **Stable fail（0/3）：18 条**
- **Decision consistency（三轮判定是否一致）：39/40 = 97.5%**

正确表述：**40 条题目中 39 条三轮判定一致，其中 21 条三轮均通过、18 条三轮均
失败；1 条在三轮间翻转。** 不使用「完美稳定」或「稳定性 100%」。

唯一不稳定的 `answer_holdout_026`（`在家办公公司给报宽带费或者话费补贴吗？`）：
Run 1 模型输出了一段元推理（「我将仔细检查[来源1]的内容」）而没有给出结论，被
判为未拒答；Run 2/3 正常输出「根据现有资料无法确定。」。这是小模型偶发的
思维过程外泄，不是评测抖动。

---

## 7. 全部失败题

Route Blind Holdout 共 62 条失败，按塌陷方向汇总（完整逐题见
`evaluation_runs/route-blind-holdout.json`）：

| 预期 route | 失败数 | 主要塌陷去向 | 典型问题 |
|---|---:|---|---|
| `direct` | 10 | 全部 → `document_only` | `早上好呀，今天开始干活了`、`嗯嗯明白了，感谢感谢`、`周末愉快，下周聊` |
| `wiki_only` | 9 | → `document_only` | `账号和权限这块我一直没搞明白，科普一下呗`、`帮我捋一遍` |
| `system_only` | 5 | → `document_only` | `查一下 SKU-C300 还剩几箱`、`SKU_B200 目前是不是已经没货了？` |
| `wiki_document` | 10 | → `document_only`(8) / `wiki_only`(2) | `先跟我讲讲远程办公整体是怎么回事，然后把每周天数上限那句原文也贴给我` |
| `wiki_system` | 9 | → `document_only`(6) / `system_only`(2) | `年假政策整体帮我捋一遍，再顺手看下 sku-c300 当前有多少` |
| `document_system` | 9 | → `document_only`(8) | `培训预算上限原文写的多少钱？SKU-B200 现在还有货没有` |
| `wiki_document_system` | 10 | → `document_only`(6) | `远程办公整体政策讲一遍，每周上限那句附上原文，顺手查下 SKU-A100 现在的库存` |

Answerability Blind Holdout 的 18 条 stable fail + 1 条 unstable：

| ID | 预期 | 实际 | 失败分类 | 说明 |
|---|---|---|---|---|
| `answer_holdout_002` | answer | answer | generation miss | 缺 `直属主管`；Run 3 更把阈值写成「超过 **2001** 元」（原文为 2000 元） |
| `answer_holdout_009` … `012` | answer | answer | route marker gap; retrieval miss | 事实全对但证据来自 document 而非 wiki，`required_source_types` 未覆盖 |
| `answer_holdout_015` `016` | answer | policy_refuse | route marker gap; policy/refusal | 路由塌陷到 `document_only` 后触发 freshness 误拒 |
| `answer_holdout_017` `018` | answer | policy_refuse | route marker gap; policy/refusal | 同上，多通道题被整体拒答 |
| `answer_holdout_019` `020` | answer | answer | route marker gap; retrieval miss | 事实对，通道缺失 |
| `answer_holdout_024` | generation_refuse | answer | generation miss | **编造**「工作日加班的加班费是按 1 倍算的」，手册从未规定倍数 |
| `answer_holdout_026` | generation_refuse | answer(1/3) | generation miss | 元推理外泄，未给结论（唯一不稳定项） |
| `answer_holdout_029` `030` `031` | policy_refuse | generation_refuse | route marker gap | 不存在的 SKU 本应在调用模型前拒答，实际走到了模型 |
| `answer_holdout_034` `036` `040` | boundary | generation_refuse | route marker gap | 应返回「当前版本仅支持库存查询。」，实际返回「根据现有资料无法确定。」 |

### 7.1 失败原因分类汇总

| 分类 | Route | Answerability（每轮） | 说明 |
|---|---:|---:|---|
| **route marker gap** | 62 | 16 | 自然表达未命中词表 |
| **policy/refusal** | 0 | 4 | 路由塌陷后 freshness 误拒 |
| **retrieval miss** | 0 | 6 | 证据来自错误通道 |
| **generation miss** | 0 | 2–3 | 编造 / 漏事实 / 元推理外泄 |

（Answerability 一列为 Run 1 计数；一条失败可同时属于多类，故合计大于 19。
40 条中共 17 条 route 判定错误，其中 16 条同时导致该题失败。）
| **citation mismatch** | 0 | 0 | 未出现 |
| **unsupported boundary** | 0 | 0（但 3 条边界题被降级为普通拒答） | 见 `034/036/040` |

**`retrieval miss` 这一类值得单独强调。** `answer_holdout_009`–`012` 四条 Wiki
概览题，模型给出的事实内容完全正确、引用编号也合法，只是证据来自 Document 通道
而非 Wiki 通道。**如果只检查「是否答对」，这四条会被记为通过。** 只有
`required_source_coverage` 把它们抓了出来。这说明多条件判定不是形式主义：
在三通道产品里，「答案对但取证通道错」是必须暴露的失败。

---

## 8. 门禁达成情况

| 门禁 | 目标 | Dev | Validation V1 | **Blind Holdout** |
|---|---|---:|---:|---:|
| Route Overall Accuracy | ≥ 90% | 95.0% PASS | 95.0% PASS | **22.5% FAIL** |
| Route Macro Accuracy | ≥ 90% | 95.0% PASS | 95.0% PASS | **22.5% FAIL** |
| Route 每类 Accuracy | ≥ 80% | 最低 80.0% PASS | 最低 80.0% PASS | **最低 0.0% FAIL（7/8 类未达标）** |
| Route Boundary Accuracy | ≥ 85% | 85.7% PASS | 86.2% PASS | **35.7% FAIL** |
| Answer Success Rate | ≥ 90% | 95.0% PASS | 95.0% PASS | **45.0% FAIL** |
| False Refusal Rate | ≤ 10% | 5.0% PASS | 5.0% PASS | **20.0% FAIL** |
| Unanswerable Refusal Rate | ≥ 90% | 100.0% PASS | 100.0% PASS | **88.9% FAIL** |
| Boundary Message Accuracy | ≥ 90% | 100.0% PASS | 100.0% PASS | **62.5% FAIL** |
| Citation Index Validity | ≥ 95% | 100.0% PASS | 100.0% PASS | **100.0% PASS** |
| Expected Fact Hit Rate | ≥ 90% | 95.0% PASS | 95.0% PASS | **75.0% FAIL** |
| 三轮判定一致率 | ≥ 90% | 不适用 | 100.0% PASS | **97.5% PASS** |
| 跨集合相似度（三项） | 见第 4 节 | — | 若按新门禁不达标 | **PASS** |

**Blind Holdout 上 11 项质量门禁中 9 项未达标**，仅 Citation Index Validity 与
三轮判定一致率通过。按纪律未修改生产代码、未降低阈值、未删题、未重写 holdout。

---

## 9. Citation 指标的口径限制

`Citation Presence` 与 `Citation Index Validity` **不等于「引用正确率」**：

- `Citation Presence` 只检查回答里是否出现 `[来源 N]` 形式的标记；
- `Citation Index Validity` 只检查 N 是否落在本次实际返回的 sources 编号范围内。

**两者都不验证被引用的那条证据是否真的支持该结论。** Blind Holdout 上
Citation Index Validity = 100%，与此同时 `answer_holdout_024` 编造了加班费倍数
并附上了 `[来源1]`——编号合法，内容无据。因此不得把这两项表述为「引用准确率」
或「可溯源率」。要衡量引用内容是否成立，需要另建 claim-level attribution 评测。

---

## 10. 发现的生产缺陷（本轮不修复）

盲测把 M7 已知的三项缺陷从「边角案例」升级为「主要失败模式」，并新增一项。

### 缺陷 1：Planner 词表无法覆盖自然中文（新证据，影响最大）

**现象**：62/80（77.5%）塌陷到 `document_only`。

**根因**：`planner._extract_signals` 依赖固定 marker 子串表。真实用户的说法不在
表内：概览意图写作 `讲讲` / `说说` / `科普一下呗` / `捋一遍` / `了解一下` /
`大方向` / `整体思路` / `整体立场`，而表里只有 `概述` / `概览` / `介绍` /
`总结` / `梳理` 等少数几个。

**建议方向**：这类问题不适合继续加词。要么改为受控的一次 LLM 路由兜底，要么
引入向量化意图匹配；继续扩充 marker 表只会把过拟合转移到下一份数据集上。

### 缺陷 2：`direct` 精确匹配整句（0/10）

**现象**：盲测中 10 条自然寒暄**无一命中**，全部走完整检索并调用回答模型。

**根因**：`_is_direct` 要求整句去掉边缘标点后完全等于 `DIRECT_MARKERS` 之一。
真人会写 `早上好呀，今天开始干活了`、`嗯嗯明白了，感谢感谢`、`周末愉快，下周聊`。

**影响**：每条寒暄浪费一次检索加一次推理；M7 中此项为 80%，属数据偏差所致。

### 缺陷 3：SKU 不被视为业务对象（`system_only` 5/10）

**现象**：`查一下 SKU-C300 还剩几箱`、`SKU_B200 目前是不是已经没货了？`
等 5 条含合法 SKU 的库存问题被路由到 `document_only`。

**根因**：`SYSTEM_OBJECT_MARKERS` 只有「库存」等名词，**没有 SKU 模式**。
`chat_orchestration.SKU_PATTERN` 能提取 SKU，但 Planner 看不到它。句中不出现
「库存」二字即认定无业务对象。

**连锁影响**：路由塌陷后，`policy_refuse`（不存在的 SKU）与 `boundary`
（未开放能力）都不再走固定短路径——用户被告知「根据现有资料无法确定」，
而不是「当前版本仅支持库存查询」，能力边界提示准确率因此降到 62.5%，
且每条多花约 11.5 秒。

### 缺陷 4（新）：marker 匹配不理解否定

**复现**：`外发资料平时要注意些什么？给我个大方向就行，不用抠原文`

**现象**：判定 `requires_exact_citation = True`，reason `document_exact`。

**根因**：`原文` 作为子串出现在「**不用抠**原文」中即被计为「要求原文」。
匹配层没有否定或范围处理。Blind Holdout 的 Exact Citation Signal Accuracy
为 70.0%（Dev 100%），此类否定与语境误判是主要来源。

### 缺陷 5（新，未文档化的能力限制）：多 SKU 查询不受支持

**复现**：`sku-a100 和 SKU-C300 现在分别还有多少？想对比一下`

**现象**：`extract_sku` 在发现多个不同 SKU 时返回 `None`，随后返回
「请提供需要查询的 SKU。」——用户已经提供了两个合法 SKU。

**说明**：评测当时的 `docs/DEMO_GUIDE.md` 未把「一次只能查一个 SKU」写为已知限制，盲写
Agent 因此按人类直觉标注为可回答。这既是产品限制也是文档缺口。

### 缺陷 6：`freshness_unsupported` 误拒（M7 已记录，此处为盲测证据）

`document_adapter._to_evidence` 把 `version` 与 `observed_at` 写死为 `None`，
而 `evidence_policy` 要求 freshness 必须由某条通道背书。路由塌陷到
`document_only` 后，含「现在/目前」的题必然被拒。Blind Holdout 的 20.0%
False Refusal Rate 主要由此产生（`answer_holdout_015`–`018`）。

---

## 11. 可对外引用的真实口径

以下表述有 **Blind Holdout V1（pre-fix audit）** 的实测支撑。
**本轮未修改任何简历文件。**

> 注意：该集合自 M8A 起为回归审计集。上述数字描述的是**修复前**的状态，
> 引用时必须说明是修复前基线；修复后在同一集合上的数字不得用于简历。

可以说：

- 「为本地三通道知识 Agent 建立了三层评测体系（开发集 / 回归基线 / 独立盲测
  holdout），共 240 条人工标注题；holdout 由一个未接触实现与既有数据集的独立
  Agent 编写，并通过字符相似度、bigram Jaccard 与实体遮蔽三重门禁验证独立性
  （与既有数据集相似度 ≥0.80 者为 0）。」
- 「盲测暴露出此前评测无法发现的问题：同一实现上，模板化数据集路由准确率
  95.0%，独立盲测集为 22.5%，说明原数据集存在明显构造偏差；80 条自然表达中
  62 条塌陷到兜底的文档检索路由。」
- 「在 40 条盲测可回答性题上连续运行三轮，通过率 54.2%，三轮判定一致率 97.5%
  （21 条三轮均通过、18 条三轮均失败、1 条翻转）。」
- 「建立多条件回答判定（事实正则命中 + 来源类型覆盖 + 引用编号合法性），
  发现 4 条题目答案内容正确但取证通道错误——仅检查‘是否答对’会漏判。」
- 「定位并根因分析 6 项可复现缺陷，含 marker 词表无法覆盖自然中文、
  SKU 未被识别为业务对象、marker 匹配不处理否定等。」
- 「工程回归测试 397 项全部通过（与上述质量指标是独立口径）。」

不可以说：

- ❌ 引用 95.0% / 97.5% 作为系统能力——那是模板相似数据上的结果。
- ❌「系统准确率 100%」或「完美稳定」。
- ❌ 把 Citation Index Validity 说成「引用准确率」（见第 9 节）。
- ❌ 把 397 项工程测试说成准确率或模型效果。
- ❌ 省略语料规模：结论基于 1 份 20 块的模拟员工手册、4 个 Wiki 页面、
  3 条库存记录，以及本地 `qwen3:4b`。

---

## 12. 限制

- 全部语料为自建模拟数据，不代表真实企业知识库的规模或噪声。
- Blind Holdout 仍只有 80 + 40 条，且由单一 Agent 编写；它证明了当前实现在
  自然表达上会失败，但不足以精确估计生产准确率的置信区间。
- 路由评测只评 Planner 的确定性规则，不含任何 LLM 兜底路由。
- 事实判定为正则匹配，不是语义等价判定；措辞差异极大但语义正确的回答可能被
  判为未命中。反之，`answer_holdout_024` 说明正则也无法识别「引用合法但内容
  编造」。
- Dev 与 Validation V1 的 `expected_fact_groups` 含裸单字符数字（如 `["5","五"]`），
  存在误命中风险。它们被保留仅为可比性，其指标不应与 Blind Holdout 并列比较。
- 极短问句不适合承载盲测独立性：相似度门禁首轮的 8 条退回全部来自此类。
- 三轮一致率在 `temperature=0` 与 JSON 结构化输出下取得，不代表开放生成设置
  下同样稳定。
- `wiki_system` 与 `wiki_document_system` 在可回答性集中仍无 `answer` 用例：
  4 个 Wiki 页面不覆盖库存等业务对象。这是语料覆盖限制，需先补 Wiki 页面。

---

## 13. M8A 修复后诊断（post-fix diagnostic on an already-seen audit set）

> **这不是新的 holdout 结果，不得用作简历指标。**
>
> 下列数字来自在 **Blind Holdout V1（pre-fix audit）** 上重跑修复后的代码。
> 该集合的每一道题在 M7.1 中都已计分并逐条分析过，修复正是针对它暴露的类别
> 编写的。因此这些数字是 **post-fix diagnostic on an already-seen audit set**，
> 只能说明「针对已知失败类别的修复是否生效」，**不能**说明泛化能力。
> 真正未见的 Blind Holdout V2 将在 M8A 代码冻结后由另一个全新 Agent 编写，
> 届时才有可对外引用的泛化数字。
>
> 数据集未改动：SHA-256 仍为 `35995ded…`（route）与 `5f55f703…`（answerability）。

Route（确定性，单次运行）：

| 指标 | pre-fix | post-fix |
|---|---:|---:|
| Overall / Macro Accuracy | 22.5% | **85.0%** |
| Boundary Accuracy | 35.7% | **82.1%** |
| Exact Citation Signal | 70.0% | **85.0%** |
| `direct` | 0.0% | **100.0%** |
| `wiki_only` | 10.0% | **90.0%** |
| `document_only` | 100.0% | 100.0% |
| `system_only` | 50.0% | **80.0%** |
| `wiki_document` | 0.0% | **70.0%** |
| `wiki_system` | 10.0% | **70.0%** |
| `document_system` | 10.0% | **80.0%** |
| `wiki_document_system` | 0.0% | **90.0%** |

非 `document_only` 题目塌陷到兜底路由：**62/80 → 1/70**。

Answerability（单轮，qwen3:4b）：

| 指标 | pre-fix(run1) | post-fix |
|---|---:|---:|
| Pass rate | 52.5% | **82.5%** |
| Answer Success Rate | 45.0% | **85.0%** |
| False Refusal Rate | 20.0% | **5.0%** |
| Unanswerable Refusal Rate | 83.3% | **100.0%** |
| Refusal Mechanism Match | 58.3% | **91.7%** |
| Required Source Coverage | 50.0% | **95.0%** |
| Expected Fact Hit Rate | 75.0% | **85.0%** |
| Route Accuracy | 57.5% | **80.0%** |
| Boundary Message Accuracy | 62.5% | 62.5%（未变） |

修复后仍失败的 7 题，无一是本轮新引入的：

| ID | 说明 |
|---|---|
| `answer_holdout_002` | 生成层漏掉 `直属主管`，与路由无关 |
| `answer_holdout_015` | **数据侧限制**：系统答「SKU_B200 现在没有货。」语义正确，但盲写的正则只列了 `0 件/无库存/缺货/售罄`，未预料「没有货」。holdout 已冻结，未改题，按失败计 |
| `answer_holdout_016` | 多 SKU：现返回正确的新边界文案，分类已修正为 `boundary`；该题人工预期是 `answer`，仍按失败计 |
| `answer_holdout_028` | `大概` 软标记在无 System 信号的子句中仍加 Wiki，属本轮未处理的残留 |
| `answer_holdout_034/036/040` | **指代型主语**：`那个单`、`我提交的转正审批`、`那个产品` 没有业务对象名词、SKU 或 ID，仍落到文档兜底。这是 Boundary Message Accuracy 未提升的唯一原因 |

耗时不作前后对比：本次单轮 P50 为 20.47s，明显高于此前同配置的 4–7s。
Planner 为纯字符串匹配，两次运行的代码路径一致，差异来自本机模型加载与资源
竞争，属环境噪声，不构成性能结论。

---

## 14. Blind Holdout V2（M8A 修复后的未见评测）

**这是本仓库唯一一份"修复后 + 未见"的评测结果。** 五套数据必须严格区分：

| # | 数据集 | 作者 | 是否见过实现／结果 | 可否代表泛化 |
|---|---|---|---|---|
| 1 | Dev | 主 Agent（已读 planner） | 是 | 否 |
| 2 | Validation V1 | 同一主 Agent | 是 | 否 |
| 3 | Blind Audit V1（pre-fix） | 独立 Agent | 编写时未见，**现已计分并用于缺陷分析** | 仅修复前基线 |
| 4 | Blind Audit V1（post-fix diagnostic） | 同上 | **修复正是针对它写的** | 否 |
| 5 | **Blind Holdout V2** | 另一独立 Agent | **未见实现、未见任何历史题、未见任何产品结果** | **是** |

引用泛化能力时**只能用 V2**。95.0%（Dev/Validation）、22.5%/54.2%（Audit V1 pre-fix）、
85.0%/82.5%（Audit V1 post-fix）都**不是** V2，不得混用。

### 14.1 冻结信息

```
c7ad0231567fd98630cdb39e48306803a8b0aa749920bbc48b3e3cf116649efb  eval_orchestrated_routes_blind_v2.json  (39708 bytes)
7ae1c0af4f82e0179ad377a6d433605da6e673d2e1d0ed5a689bcf3fd1bc9d89  eval_answerability_blind_v2.json        (23762 bytes)
```

- 冻结 commit：`65b402c203fe389742b6bf87b81bf32cba081e73`
- 产品代码 commit：`df3b4c9f3143c803a991ac0afc6f734b74a25139`（计分期间零修改）
- 清单：`evaluation_runs/blind-v2-freeze-manifest.json`（冻结时 `product_scoring_started: false`）

### 14.2 独立性门禁（计分前完成，11 项全过）

对比 6 份历史数据集 + M8A 回归测试表（AST 静态提取，未导入执行），共 **441 条**参考问题，
外加 V2 两份之间的交叉检查。

| 比较 | 最高相似度 | ≥0.85 | ≥0.80 | entity-swap | 内部模板 >2× |
|---|---:|---:|---:|---:|---:|
| Route V2 vs 全部参考 | 0.727 | 0（限 0） | 0（限 4） | 0 | 0 |
| Answerability V2 vs 全部参考 | 0.759 | 0（限 0） | 0（限 2） | 0 | 0 |
| Route V2 × Answerability V2 | 0.759 | 0（限 0） | 0（限 4） | 0 | — |

> V2 的**第一版**在同一门禁上失败：13 题超阈值，含 2 条逐字重复（`哈喽`、`婚假可以休几天？`）
> 与 2 条撞上 M8A 回归测试表。已退回作者重写 14 个 ID 后重测通过。本节数字来自重写版。

### 14.3 Route V2 结果（确定性，单次计分）

| 指标 | 值 | 门禁 | 结果 |
|---|---:|---|---|
| Overall Accuracy | **71.2%**（57/80） | ≥90% | FAIL |
| Macro Accuracy | **71.2%** | ≥90% | FAIL |
| Steps Exact Match | 71.2% | — | — |
| Boundary Accuracy | **66.7%**（30/45） | ≥85% | FAIL |
| Freshness Signal | 97.5% | — | — |
| Exact-citation Signal | 80.0% | — | — |

| route | 准确率 | 门禁 ≥80% |
|---|---:|---|
| `direct` | 80.0% | PASS |
| `wiki_only` | 80.0% | PASS |
| `document_only` | 100.0% | PASS |
| `system_only` | 80.0% | PASS |
| `wiki_document` | **40.0%** | FAIL |
| `wiki_system` | 70.0% | FAIL |
| `document_system` | **50.0%** | FAIL |
| `wiki_document_system` | 70.0% | FAIL |

混淆矩阵（行=人工预期，列=实际）：

| 预期 \ 实际 | direct | wiki_only | doc_only | sys_only | wiki_doc | wiki_sys | doc_sys | wds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `direct` | **8** | 0 | 2 | 0 | 0 | 0 | 0 | 0 |
| `wiki_only` | 0 | **8** | 1 | 0 | 1 | 0 | 0 | 0 |
| `document_only` | 0 | 0 | **10** | 0 | 0 | 0 | 0 | 0 |
| `system_only` | 0 | 0 | 2 | **8** | 0 | 0 | 0 | 0 |
| `wiki_document` | 0 | 4 | 2 | 0 | **4** | 0 | 0 | 0 |
| `wiki_system` | 0 | 0 | 0 | 0 | 0 | **7** | 3 | 0 |
| `document_system` | 0 | 0 | 0 | 5 | 0 | 0 | **5** | 0 |
| `wiki_document_system` | 0 | 0 | 0 | 0 | 0 | 2 | 1 | **7** |

实际路由分布：`document_only` 17、`system_only` 13、`wiki_only` 12、`document_system` 9、
`wiki_system` 9、`direct` 8、`wiki_document_system` 7、`wiki_document` 5。
分布均衡，**没有再出现 V1 那种 62/80 塌陷到兜底路由的情况**。

### 14.4 Answerability V2 结果（三轮，真实本地链路）

三轮逐题判定完全一致，per-run 指标相同：

| 指标 | Run 1 | Run 2 | Run 3 | 均值 | 门禁 | 结果 |
|---|---:|---:|---:|---:|---|---|
| Pass rate | 80.0% | 80.0% | 80.0% | **80.0%** | — | — |
| Answer Success Rate | 85.0% | 85.0% | 85.0% | **85.0%** | ≥90% | FAIL |
| False Refusal Rate | 0.0% | 0.0% | 0.0% | **0.0%** | ≤10% | PASS |
| Unanswerable Refusal Rate | 91.7% | 91.7% | 91.7% | **91.7%** | ≥90% | PASS |
| Refusal Mechanism Match | 91.7% | 91.7% | 91.7% | 91.7% | — | — |
| Boundary Message Accuracy | 50.0% | 50.0% | 50.0% | **50.0%** | ≥90% | FAIL |
| Citation Presence Rate | 95.2% | 95.2% | 95.2% | 95.2% | — | — |
| Citation Index Validity | 100.0% | 100.0% | 100.0% | **100.0%** | ≥95% | PASS |
| Required Source Coverage | 90.0% | 90.0% | 90.0% | 90.0% | — | — |
| Expected Fact Hit Rate | 90.0% | 90.0% | 90.0% | **90.0%** | ≥90% | PASS |
| Route Accuracy | 82.5% | 82.5% | 82.5% | 82.5% | — | — |

耗时 P50 / P95 / 最大：3.77s / 11.79s / 13.42s。

**稳定性（严格口径）**

- **stable pass（3/3 通过）：32 条**
- **unstable（1/3 或 2/3）：0 条**
- **stable fail（0/3 通过）：8 条**
- **decision consistency：40/40 = 100%** —— 这是"三轮判定是否一致"，**不是正确率**。
  8 条稳定失败在三轮里都失败，一致性 100% 不代表它们通过。

### 14.5 全部失败题

**Route V2（23 条）**，按原因归类：

| 原因 | 条数 | 说明与例子 |
|---|---:|---|
| **精确性改写未识别为原文诉求** | 11 | `要精确的`／`准确说法`／`准确表述`／`准确天数`／`一字不差的`／`别搞错`／`必须精确`／`原样条文` 都不在 exact 表，document 通道丢失。`route_v2_063/064/067/068/070` 退化为 `system_only`，`042/045/047/048/073` 退化为 wiki 侧 |
| **概览动词缺口** | 5 | `总览`（050、075）、`概括`（018）、`来龙去脉`（052）、`从头到尾`（041）、`讲清楚`（055）不在概览表 |
| **社交词表缺口** | 2 | `回头聊`／`先忙去了`／`明天再说` 未覆盖，`route_v2_005/006` 未判为 direct |
| **业务对象需旁证词** | 2 | `库存还有货吗`（035）无实时动词；`apr-3001 的审批走完了没`（038）审批单号未识别 |
| **政策名词跨子句仍夺路** | 3 | `远程办公那套规定`／`请假制度的来龙去脉` 中 `规定`／`制度` 使 wiki 丢失并强制 document（051/052/055） |
| **否定窗口只向前看** | 1 | `具体条款先别给我`（015）：否定词在标记**之后**，未被捕获 |

**Answerability V2（8 条，三轮一致）**：

| ID | 预期→实际 | 原因归类 | 要点 |
|---|---|---|---|
| `answer_v2_006` | answer→answer | generation miss + citation mismatch | 模型**原样复述了问题**，未作答、无引用 |
| `answer_v2_018` | answer→answer | route marker gap + retrieval miss | `几小时内必须上报、报给谁` 无 exact 标记，document 通道丢失，只答了库存 |
| `answer_v2_019` | answer→answer | route marker gap + retrieval miss | `原样给我` 未识别，document 通道丢失（事实恰好由 wiki 覆盖） |
| `answer_v2_028` | generation_refuse→answer | generation miss | **编造**「加班费按 1 倍计算」并附 `[来源1]`。语料从未规定倍数 |
| `answer_v2_034` | boundary→policy_refuse | route marker gap | `现在这边货还够吗` 指代型主语 |
| `answer_v2_035` | boundary→generation_refuse | route marker gap | `库存这边帮我看一眼` —— `看一眼` 不是实时动词 |
| `answer_v2_038` | boundary→policy_refuse | route marker gap | `ord-1001` 订单号未识别为业务对象 |
| `answer_v2_040` | boundary→generation_refuse | route marker gap | `subject-001` 主体 ID 未识别 |

`answer_v2_028` 与 Audit V1 的 `answer_holdout_024` 是**同一条编造**（加班费倍数），
在两份互不相关的数据集上独立复现，可判定为稳定的生成层缺陷而非偶发。

### 14.6 与旧数据的口径差异

| 数据集 | Route | Answerability |
|---|---:|---:|
| Dev / Validation V1（构造偏差） | 95.0% | 97.5% |
| Blind Audit V1（pre-fix） | 22.5% | 54.2% |
| Blind Audit V1（post-fix，已见） | 85.0% | 82.5% |
| **Blind Holdout V2（未见）** | **71.2%** | **80.0%** |

Route 上 85.0% → 71.2% 的 **13.8 个百分点**差距，量化了 M8A 修复中"对已见审计集特化"的部分：
在完全未见的自然表达上，泛化收益低于在审计集上观察到的数值。
Answerability 差距较小（82.5% → 80.0%），因为其失败更多来自生成层而非路由词表。

### 14.7 是否适合写入简历

**适合，但只能引用 V2，且必须带语料规模限定。** 门禁未全过（Route 4/8 类未达 80%，
Answerability 2 项未达标），因此不能声称"达标"或"生产就绪"。

可以写：

- 「设计三层评测体系并引入跨集合相似度门禁（字符 SequenceMatcher、bigram Jaccard、
  实体遮蔽模板签名）；独立盲测集第一版因 13 题超阈值（含 2 条逐字重复）被门禁拦回重写，
  重写版最高相似度 0.73/0.76、≥0.80 为 0 后方才冻结计分。」
- 「在冻结的独立盲测集上（80 条路由 + 40 条可回答性，作者未见实现、未见历史题、
  未见任何产品结果）：路由准确率 71.2%，可回答性三轮通过率 80.0%，
  三轮判定一致率 100%（32 条三轮均通过、8 条三轮均失败、0 条翻转），
  误拒率 0%，引用编号合法率 100%。」
- 「同一修复在已见审计集上为 85.0%，在未见盲测集上为 71.2%，据此量化评测集特化程度。」
- 「定位并复现跨数据集的稳定生成层缺陷（同一条加班费倍数编造在两份独立数据集上重现）。」

不可以写：

- ❌ 用 95.0% / 22.5% / 85.0% 冒充 V2。
- ❌ 称任何门禁"达标"或"通过"。
- ❌ 把 decision consistency 100% 说成准确率或稳定性 100%。
- ❌ 省略语料规模：1 份 20 块模拟手册、4 个 Wiki 页面、3 条库存记录、本地 `qwen3:4b`。

---

## 15. 复现命令

```powershell
.\.venv\Scripts\python.exe -m py_compile `
  evaluate_orchestrated_routes.py `
  evaluate_answerability.py `
  check_evaluation_overlap.py

.\.venv\Scripts\python.exe -m unittest discover -v

.\.venv\Scripts\python.exe check_evaluation_overlap.py

.\.venv\Scripts\python.exe evaluate_orchestrated_routes.py `
  --dataset eval_orchestrated_routes_holdout.json `
  --output evaluation_runs\route-blind-holdout.json

.\.venv\Scripts\python.exe evaluate_answerability.py `
  --dataset eval_answerability_holdout.json `
  --runs 3 `
  --output-dir evaluation_runs\answerability-blind-holdout
```

`run_agent_evaluations.ps1` 按冻结顺序串联全部步骤（静态检查 → schema →
相似度门禁 → 路由 → 可回答性）。两个评测器都支持 `--validate-only`。
评测器以非 0 退出表示**门禁未达标**，不是脚本崩溃。

原始结果位于 `evaluation_runs/`。
