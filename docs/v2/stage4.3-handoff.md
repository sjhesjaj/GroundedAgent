# GroundedAgent V2 / Stage 4.3 — After-sales Policy + Wiki Lifecycle

实现基线：`main == origin/main == bc05c693bc89c1ace67415710141006fea1b8ab2`；fetch 后核对一致、工作区干净，再创建 `stage4-policy-wiki`。实现验收完成后仅进行 Stage 4.3 commit / push / PR 交付，禁止 merge。未进入 Stage 4.4。

## 交付内容

`policy source → deterministic front matter → existing Wiki compiler → DRAFT → diff → publish → published catalog → PolicyRecord / Evidence` 已实现，rollback 复用原有发布指针。未新增售后 repository；所有源版本、build、发布和回滚仍属于 `WikiRepository`。

新增代码：

- `aftersales/policy_source.py`：front matter 闭合 schema、解析、规范序列化。
- `aftersales/policy_lifecycle.py`：完整 corpus 编译、snapshot 完整性检查、字段/正文 diff、冻结和校验。
- `aftersales/policy_catalog.py`：published snapshot、业务选择、Evidence 渲染、policy_ref 校验。
- `aftersales/policy_cli.py`：compile / diff / publish / rollback / freeze / verify。
- `tests/test_v2_policy_lifecycle.py`：88 项验收测试（含 PR #10 review 的 5 项新增回归）。
- `tools/check_stage43_mutations.py`：隔离源副本上的 11 个语义 mutation。

新增数据：`policy_sources/*.md` 六条规则；`wiki_pages/sample_aftersales_wiki.json` 售后 fallback；`wiki_pages/aftersales_frozen/` 可读取的完整发布快照，包含现有格式的 build、文档快照、manifest、current pointer。

修改代码：`aftersales/policy.py` 增加 priority；`registry.py` 默认绑定真实 published adapter；`wiki_maintenance/models.py` / `repository.py` 增加可选编译 provenance；`wiki_runtime.py` 支持显式 hold-as-draft，普通上传仍默认自动发布；`prompts.py` 领域迁移；`chat_orchestration.py` 售后 fallback；`rag.py` 只增补售后名词。`orchestration/contracts.py`、Evidence Policy V2、WikiClaim schema 和业务 Evidence schema 均无需修改。

## Source / front matter schema

一个 Markdown 文件对应一条 policy。`---` 围栏内为严格 **JSON front matter**，不支持 YAML 的隐式类型转换。所有键显式必填：

```text
policy_id: safe string
version: safe string
title: nonempty string
rule_type: return_window | exchange_window | non_returnable | handoff
scope: unique string[]                 # [] = 所有品类，精确匹配
priority: int                         # bool/string/float 均拒绝
params: closed object
effective_from: aware ISO-8601 string
effective_to: aware ISO-8601 string | null
source_doc: safe document identity
locator: section:<真实二级正文标题>
provenance: {issuer: nonempty string, revision: nonempty string}
```

window params 恰好为 `window_days`（正整数）、`start_event=delivered`、`counting_rule=natural_days_from_next_day`、`utc_offset`。non_returnable params 恰好为 `reason_code`；handoff params 恰好为 `trigger`，均为非空安全标识字符串。缺键、未知键、重复 JSON 键、错误类型、无时区、倒置/空窗口、无效 locator 均抛错。

编译先规范化 header，再将 header + 正文存入**同一 DocumentSnapshot**。文档版本和完整 content hash 因此包含结构字段变化。只有正文 spans 进入 Wiki 编译器。catalog 从 build 锁定的 snapshot 的 header 重新确定性解析 PolicyRecord，不另存参数副本。读取时检查 span 内容 hash、span identity/source/顺序、document hash 和 document identity。正文和模型输出绝不回填 params。

## Priority 与两条时间轴

`PolicyRecord.priority` 是业务 precedence；旧 fixture 可省略并得到 0，正式 source 不可省略。Evidence authority 固定为 document 的 90，不参与规则 precedence。

选择顺序：按 `as_of` 的 `[effective_from, effective_to)`、rule_type、category scope 过滤 → 取最大 priority → 只看最高层。最高层一条则选用，多条且规范 JSON params 相等则共同支持；不同则抛出 `PolicyPrecedenceConflict`，不选择任意一条。下层冲突不影响最高层。不使用 scope 具体程度、version 排序、文件顺序或发布时间作为 tie-breaker。

`category=None` 的直接 select 只选择全品类规则，不假定订单品类。query 的词法召回仅识别相关规则类型；precedence 总是读取该类型的全部规则，不能以“标准退货”这样的关键词绕过促销。query 明确提到已知品类时按该品类选择；泛咨询展示适用的品类分支，Evidence 附带 scope 和 selected_categories。此处未接入 Planner，也未验证自然语言召回质量。

Wiki `created_at`、current pointer 的 `published_at`、publish/rollback 的 BuildRecord.updated_at 继续由现有 repository 读墙钟；active policy 判断只读 context.clock 提供的业务时刻。即使墙钟 publication 在 2040，virtual_now 在 2026 或 2031，仍按业务 effective window 选择。

## Corpus

| Policy | 规则 | Scope | Priority | Effective |
|---|---|---|---:|---|
| standard-return | 7 自然日退货窗口 | 全部 | 10 | 2026-01-01 起 |
| standard-exchange | 15 自然日换货窗口 | 全部 | 10 | 2026-01-01 起 |
| apparel-exchange | 30 自然日换货窗口 | 服装 | 20 | 2026-01-01 起 |
| custom-non-returnable | 定制商品不适用无理由退货 | 定制 | 10 | 2026-01-01 起 |
| quality-handoff | 质量争议需人工处理 | 全部，条件为 quality_dispute | 10 | 2026-01-01 起 |
| november-promo-return | 15 自然日促销退货窗口 | 全部 | 100 | [2026-11-01, 2026-12-01) |

时间偏移均 +08:00。scope 的“服装”与现有业务源分类值一致。窗口规则只说明时限，不宣称满足所有退货资格。

## Lifecycle 与可复现操作

`compile_policy_draft(repo, sources)` 接受完整替换 corpus，始终产生 DRAFT，从当前发布 build 取得页版本复用信息。默认调用现有 `assemble_page_from_spans`：每条 source title 为一个 topic，正文原样成为 claims，不需要网络或模型。也可注入现有 WikiModel，走 `compile_wiki_fast`；必须提供 provider/model/config provenance，front matter 仍不进入模型。

`WikiRuntime` 普通上传默认 `hold_as_draft=False`，模块级 `RUNTIME = WikiRuntime()` 保留 upload → publish 行为；显式 `hold_as_draft=True` 时保留草稿，status 提供 `draft_build_id`。自动发布、批次失败恢复和跨文档 retirement 机制不变。`compile_policy_draft()` 仍始终创建 DRAFT。正式售后来源使用下列 policy CLI / Python API；没有新增 API publish / rollback endpoint。

在仓库根目录，使用一个新的本地 root（不要对冻结包运行 publish / compile）：

```powershell
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root tmp/policy-demo compile
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root tmp/policy-demo publish build-0001
# 在 corpus 的工作副本修改促销规则，再编译整个 corpus：
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root tmp/policy-demo compile --sources <source-copy>
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root tmp/policy-demo diff build-0001 build-0002
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root tmp/policy-demo publish build-0002
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root tmp/policy-demo rollback build-0001
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root tmp/policy-demo freeze
```

diff 包含 policy 新增/删除的 before/after，version / params / priority / effective window 等 header 字段的完整值，以及现有 WikiDiff 和 page/claim 正文 before/after；相同 old/new 输出稳定排序。

真实临时仓库演示记录在 `stage4.3-lifecycle-demo.json`：当前 build-0001 的促销为 15 天；修改源文件为 version=2、20 天、priority=110；build-0002 DRAFT 时 runtime 仍为 15 天；发布后 20 天；rollback 后 15 天。现有只读 demo DB 同一订单 ORD-1001 / SF1001，在 2026-11-15 签收已 10 天，命中 15 天促销并 within_return_window=true；2026-12-01 签收已 26 天，命中标准 7 天并 false。业务 DB total_changes 不变；每条 derived 的 policy_refs 均通过真实 Evidence 校验。此演示是组件链路实测，不是完整聊天、Planner 或正式 Eval 验证。

## Runtime tool 与 provenance

registry 仍恰好五个只读工具。`search_after_sales_policy` 只有 `query: string`，additionalProperties=false，side_effect=false，不接收身份、build、draft 或 as_of 参数。as_of 来自受信任 context.clock；query 中写 build id 或时间不能改变所读 publication 或时钟。

默认 adapter 通过 `DEFAULT_AFTERSALES_POLICY_ROOT` 读取 committed `wiki_pages/aftersales_frozen/`，与当前工作目录无关。catalog 以 `WikiRepository(..., create_directories=False)` 读取既有 publication，不建目录、不创建文件、不 publish、不改变冻结包。可编辑 repository 仍可显式注入其他 root。未部署 policy build、旧 V1 build、文件损坏均为 `PolicyCatalogUnavailable`，经执行器成为 ERROR，绝不伪装 EMPTY。只有已成功读取发布规则且无查询匹配时返回 EMPTY。precedence 冲突经执行器显示 `PolicyPrecedenceConflict` 类型的 ERROR，不能形成可用 policy evidence。

默认直接运行冻结包（此构造不写文件）：

```python
from aftersales.registry import build_runtime_registry
registry = build_runtime_registry()
```

输出为 DOCUMENT Evidence，每个结构字段一个 locator，例如 `policy:november-promo-return#window_days`。metadata.value 是整数 15，不需要解析 content。metadata 附带 policy_id/version/build_id/rule_type/source_doc/source_locator/source_version/source_digest/provenance/effective_from/effective_to/priority/scope；未 dump repository。观察时刻来自业务 Clock，observation_id 由既有 executor 关联。

`validate_policy_refs(derived.policy_refs, snapshot=..., evidence=...)` 通过 `snapshot.lookup(ref)` 获取权威 PolicyRecord，以 snapshot.source_versions / snapshot.provenance 为来源锚点。全部结构化 Evidence 必须匹配 policy_ref、policy_id、version、build_id、rule_type、source_doc、source_locator、source_version、source_digest、provenance、priority、effective_from/to、scope、field 和 value。错 build、错 version、伪造来源版本/摘要/provenance、修改参数或缺字段均抛错；旧 snapshot 配新 Evidence 也拒绝。调用方不能再仅提供 records 作为来源锚点。此函数只检查 provenance correspondence，不自动推导 eligibility，也没有修改 Evidence Policy V2。

## Frozen build

冻结目录：`wiki_pages/aftersales_frozen/`；manifest：`stage4.3-frozen-manifest.json`。

- build_id：`build-0001`（本地 repository 身份；跨 repository 必须同时比较 digest）。
- content_digest：`191f4b6d9bc885cb1b161557319186511fcbc4c24014900cb7dd3f972e02fef5`。
- policy_source_digest：`ffb347b259ce619c9573fa343d44021eb0fdd78dd652277bee3c394e37766639`。
- manifest 保留所有 source document version 和完整 hash、编译后页面、parser/compiler identity、相关实现源码 digest；注入模型时另有 provider/model/config 和 prompt digest。
- content_digest 不包含 build_id、base_build_id、created_at、published_at 或 rollback 时间。政策字段、正文、页面与编译 provenance 变化会改变 digest；相同输入在新 repository 和不同审计时间下可复核。source hash 沿用既有 span 规范化规则，不宣称是原文件字节 hash。

```powershell
.\.venv\Scripts\python.exe -m aftersales.policy_cli --root wiki_pages/aftersales_frozen verify docs/v2/stage4.3-frozen-manifest.json
```

没有创建 eval-env-v2 或任何新评测集；后续正式 Eval 可消费这个冻结包，不能将其误认为已经完成 Eval。

## Prompt / domain changes 与既有测试夹具

Wiki decision/topic/page 三处提示词改为电商售后语境；售后草稿和临时促销通知不再因“与公司制度无关”被排除。输出 JSON schema、一次 repair、source span 检查、数字来源校验等保持原逻辑。chat fallback 指向新售后样例；旧企业 Wiki 文件只留给原有测试显式使用。rag 的通用名词机制保留，只增补退货/换货/签收/物流/售后等名词。

原测试断言均保留，只有以下夹具装配变更：

1. `test_wiki_runtime.py` RuntimeTestCase.runtime 和 import-purity 构造、`test_cross_document_supersede.py` 的运行时、`test_upload_upsert.py` 的两个运行时：显式 `hold_as_draft=False`，继续验证原自动发布/回退/retirement 机制。Stage 4.3 同时覆盖不传 flag 的生产默认自动发布和显式 `hold_as_draft=True` 的 DRAFT 行为。
2. `test_orchestrated_chat.py` 基础 fixture 显式注入原企业样例，保留原有来源映射/传输断言，避免生产 fallback 的领域改变破坏基础设施检查。
3. `test_v2_tool_executor.py::test_policy_adapter_not_ready_is_its_own_error` 显式注入 NotReady fixture，继续检查错误分类。生产默认不再依赖该 fixture。

## 验证与技术债

最终计数与源码 SHA 绑定记录在 `stage4.3-validation.json`；mutation 结果在 `stage4.3-mutation-results.json`。Git 状态快照仅保留在本地忽略目录 `tmp/`，不作为长期仓库产物提交；最终 SHA 和 PR 状态以提交交付报告为准。

| 检查 | 实测结果 | Exit |
|---|---:|---:|
| 原 1437 项 + Stage 4.3 88 项，全量 unittest discover | 1525 passed / 0 failures / 0 errors / 0 skips | 0 |
| Stage 4.3：tests.test_v2_policy_lifecycle 单独运行 | 88 passed / 0 skips | 0 |
| Wiki infrastructure：test_wiki_*.py 单独运行 | 255 passed | 0 |
| V1 Evidence Policy 单独运行 | 71 passed | 0 |
| isolated mutation control | 全部指定测试通过 | 0 |
| generic WikiRuntime 默认改回 hold_as_draft=True | killed | mutant=1 / runner=0 |
| 去掉 source_version 校验 | killed | mutant=1 / runner=0 |
| 去掉 source_digest 校验 | killed | mutant=1 / runner=0 |
| 去掉 provenance 校验 | killed | mutant=1 / runner=0 |
| 默认 catalog root 改回空 data/wiki | killed | mutant=1 / runner=0 |
| publication time 被当作 effective_from | killed | mutant=1 / runner=0 |
| priority ignored | killed | mutant=1 / runner=0 |
| lower priority wins | killed | mutant=1 / runner=0 |
| draft visible | killed | mutant=1 / runner=0 |
| rollback no-op | killed | mutant=1 / runner=0 |
| body overwrites front matter params | killed | mutant=1 / runner=0 |
| 冻结包 CLI verify | verified=true / build-0001 | 0 |
| git diff --check | 无 whitespace error | 0 |

全量运行使用 `unittest.defaultTestLoader.discover('.')` 与 `TextTestRunner(verbosity=2)`，与 `python -m unittest discover -v` 相同发现入口，并直接记录 result.testsRun / failures / errors / skipped。mutation 合计 11/11 killed（原 6 + PR #10 review 5）。没有删除或 skip 既有测试。

其他复现命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_wiki_*.py' -q
.\.venv\Scripts\python.exe -m unittest tests.test_evidence_policy -q
.\.venv\Scripts\python.exe tools/check_stage43_mutations.py
git diff --check
```

Provider 未迁移：**TD: Wiki compiler still uses current compiler path; formal Stage 4 eval consumes a frozen build.** 默认售后冻结构建使用现有确定性 verbatim assembler；注入模型时仍可用当前 Ollama WikiModel。没有声称做过真实在线模型编译验证。

**category evidence 与 delivered_at evidence 属于同一订单的结构化证明，必须在 Stage 4.4 Baseline 接入之前解决。** 本阶段没有扩大业务 Evidence schema。

沿用 repository 的单进程、单 writer 限制；没有引入多 writer 事务。参数只认 front matter，正文与参数在语义上是否一致仍需审阅。词法查询可能返回多个相关规则类型，测试证明结构、时间、precedence 和来源闭环，不代表自然语言检索准确率或完整售后资格判断。

未实施 Planner/Router、baseline、dev/validation/holdout、Eval Reset、Tool Loop、V2 主聊天 API、frontend、业务写动作、Guard 或 approval。
