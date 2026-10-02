# GroundedAgent V2 Stage 6.4B 设计：数据集隔离与作者 bundle 冻结

本文细化冻结设计 `docs/v2/stage6-design.md` §21（数据集与 holdout 隔离）与 §22 Stage 6.4 的后半段，不改变冻结设计中的任何决定。它约束的是**流程**：谁在什么时候能看到什么、哪些文件在什么时候冻结、每一步留下什么可核验的证据。

## 0. 基线

- Stage 6.4A（评测核心，PR #25）以 merge commit 合入：`main` = `44efea23ae12fb9ee659139afec152d75768389e`。它包含 Stage 6 的全部规格文件、只依赖标准库的 case 契约、harness、B/F 比较器、L1–L6、指标与 oracle，并已通过 review（6.4A.1 修正了分布计划的最终结论词表与私有工件边界测试）。
- 本阶段开始时仓库中没有任何 Stage 6 数据集、私有 holdout、作者 bundle、封存 manifest 或开封工具，也没有任何正式 LLM 运行。

## 1. 步骤顺序（§21 的 1–8）

| # | 步骤 | 产物 | 谁 | 状态 |
|---|---|---|---|---|
| 1–4 | 冻结领域 / 动作规格、规则语料、case schema 与契约、seed 与 action schema | 6.4A 的规格文件 | 实现会话 + review | 6.4A 已合入 |
| 5 | 生成作者 bundle：输入 manifest + 导出工具 + 作者 brief + 回执工具 | `eval/v2/stage6-holdout-input.manifest.json`、`tools/export_v2_stage6_author_bundle.py`、`docs/v2/stage6-author-brief.md`、`eval/v2/stage6_dataset_receipt.py` | 实现会话 + review | **本 PR** |
| 5′ | bundle 冻结 | 本 PR 的 merge commit（下称 freeze commit） | 人 | 本 PR 合入时 |
| 6 | 隔离的作者上下文编写 holdout | 仓库外的 holdout 文件与回执 | 隔离作者（由人启动） | 下一步 |
| 7 | 封存：仓库只登记 `eval/v2/stage6-holdout.manifest.json`，并预先提交开封工具 | 封存 manifest、`tools/unseal_v2_stage6_holdout.py` | 实现会话 + review | 作者完成之后 |
| 8 | 隔离作者编写 DEV、VALIDATION；oracle 路径检查标签 | `eval/v2/stage6-dev.json`、`eval/v2/stage6-validation.json` 与各自回执 | 隔离作者；oracle 由实现会话运行 | 封存之后 |
| 6.4 退出 | Guard / Gateway / 渲染器 / 评分器冻结 | annotated tag `v2-stage6-action-core` | 人 | oracle 检查之后 |

每一步都在前一步合入之后开始。本 PR 只做第 5 步。

## 2. 作者 bundle

### 2.1 内容（27 个文件）

| 文件 | 为什么作者需要它 |
|---|---|
| `docs/v2/stage6-author-brief.md` | 作者的任务、可用材料、禁止事项、自查与输出（§3） |
| `docs/v2/stage6-domain-spec.md` | Stage 6 的领域与评测规格，编写时的唯一权威 |
| `docs/v2/holdout-domain-spec.md` | Stage 6 沿用的 Stage 4/5 只读领域（实体、身份、时间、规则语义、证据、追问、读取故障） |
| `eval/v2/spec/stage6-case.schema.json`、`stage6-actions.json`、`stage6-scenarios.json`、`stage6-final-outcomes.json`、`stage6-holdout-plan.json` | Stage 6 的格式、契约、词表与分布约束 |
| `eval/v2/spec/case.schema.json`、`slots.json`、`personas.json`、`archetypes.json`、`final-outcomes.json` | Stage 6 格式复用的定义；Stage 6 契约的词表一致性检查读取它们 |
| `eval/v2/case_contract.py`、`eval/v2/stage6_case_contract.py` | 只依赖标准库的契约检查器（Stage 6 契约按路径加载冻结的 Stage 4/5 检查器） |
| `eval/v2/stage6_dataset_receipt.py` | 只依赖标准库的回执工具（§4） |
| `aftersales/schema.sql`、`aftersales/action_schema.sql` | 表结构：编写补丁与预期终态时对照列名 |
| `system_fixtures/aftersales_demo_seed.sql`、`system_fixtures/aftersales_stage6_seed.sql` | 每个 case 叠加补丁的冻结业务数据；契约用它们检查版本递增、主键存在与外键 |
| `policy_sources/*.md`（6 个）、`docs/v2/stage4.3-frozen-manifest.json` | 已发布的规则语料与发布清单 |

### 2.2 有意排除

| 排除 | 理由 |
|---|---|
| `docs/v2/stage6-design.md`、本文及其他设计文档 | 描述实现架构、控制循环与提示规则；作者只应依据领域规格 |
| `eval_v2/`、`aftersales/*.py`、`orchestration/`、根目录模块 | 实现 |
| `tests/`、`tools/`、`HANDOFF.md`、`AGENTS.md` | 实现细节、开发记录与失败分析 |
| `eval/v2/spec/holdout-plan.json`、Stage 4/5 的 dev / validation / holdout 及其回执与结果 | 属于 Stage 5 的数据与分布，与 Stage 6 无关，且可能诱导作者模仿已有 case |
| 任何 Stage 6 数据集、结果或 oracle 输出 | 作者之间互相隔离；作者不运行 oracle |

`holdout-domain-spec.md` 的文件表中提到 Stage 5 的 `holdout-plan.json`：它不在 Stage 6 bundle 中，Stage 6 的分布以 `stage6-holdout-plan.json` 为准（作者 brief §3 说明）。

### 2.3 输入 manifest 与冻结规则

- `eval/v2/stage6-holdout-input.manifest.json`（schema `v2-stage6-holdout-input-manifest/1`）列出每个文件的路径、LF 规范化后的 sha256 与字节数；`content_digest` 只覆盖 schema 与 (path, sha256)，与 commit、时间无关；`base_commit` 是冻结分支的起点（`44efea2`），不是 freeze commit。
- 导出工具 `tools/export_v2_stage6_author_bundle.py` 与 Stage 4/5 的导出工具机制相同（新文件，旧工具与旧 manifest 都不改）：显式允许清单 + 路径 token 禁止表（另加 `design`、`prompt`、`oracle`、`scoring`、`results`、`harness`、`loop`、`agent`）+ 代码只允许三个标准库检查器；导出前核对每个 sha256 与摘要，任何不一致都拒绝且不写入；目标目录必须在仓库之外且为空；bundle 根目录另写 `bundle-manifest.json`。
- **冻结规则：** 本 PR 合入后，27 个文件在 Stage 6 holdout 开封之前不可修改。封存 manifest 与开封工具会重新核对它们（与 Stage 4/5 相同的做法），所以任何修改都会阻止开封。若 review 之后发现必须修改其中的文件，只能在封存之前、以新的 freeze commit 重新冻结，并在 HANDOFF 记录原因；holdout 一旦以某个 freeze commit 编写，就不能再改那些文件。
- 评测实现（`eval_v2/` 的 harness、比较器、评分器）不在 bundle 中，但 Stage 6 harness 加载的是同一个 `eval/v2/stage6_case_contract.py`，所以契约的语义随 bundle 一起冻结。

### 2.4 信任锚与只读 bundle（6.4B.1 冻结）

review 发现：如果回执工具只信任 bundle 自带的 `bundle-manifest.json`，一个被改过的清单可以删掉、增加或重算某个文件的哈希并重算自己的摘要；多出来的文件也只会被忽略。以下规则随 bundle 一起冻结：

1. **bundle 清单不是它自己的信任锚。** 期望的输入 bundle 摘要由启动作者的人在带外提供，取自 review 之后冻结的仓库清单（`eval/v2/stage6-holdout-input.manifest.json` 的 `content_digest`，即导出工具输出的值）。回执工具重新计算 bundle 的摘要，必须与 `--expected-bundle-digest` 完全相等（64 位小写十六进制），否则拒绝；回执中的 `input_bundle_digest` 就是这个经过核对的值。作者不得从本地 bundle 清单读取或推算期望摘要。
2. **文件集合必须精确。** 导出的 bundle 目录中恰好是清单列出的文件加 `bundle-manifest.json`：没有其他文件（额外的 `.md` / `.json` / `.py`、其他数据集、实现文件、旧回执、临时输出），没有其他目录（包括空的 `__pycache__`），没有符号链接；每个文件的 LF 规范化 sha256 与字节数都与清单一致。多出的文件不是被忽略，而是拒绝。这使回执中的 `frozen_bundle_only = true` 有机器可核验的依据。
3. **清单结构严格校验。** `schema`、`input_manifest_schema` 精确匹配，`hash_normalization == "crlf-to-lf"`，字段集合精确；`files` 非空；路径是唯一、排序、普通的相对路径（不得绝对、不得 `..`、不得反斜杠或盘符、不得以 `.` 开头）；sha256 为 64 位小写十六进制；字节数为非负整数并与实际一致；摘要能精确重算。任何畸形字段都映射为拒绝，不会以异常崩溃。
4. **bundle 只读。** 数据集与回执都必须解析到 bundle 之外（跟随 `..` 与符号链接之后判断）；回执工具不在 bundle 中写任何东西：它以 `sys.dont_write_bytecode` 加载检查器，不产生 `__pycache__`。作者的自查与回执命令都用 `python -B`；以编程方式导入检查器时，导入方的进程也必须用 `-B`（导入时的字节码缓存由导入方决定）。
5. **启动作者时提供恰好三样东西**：split、freeze merge commit、期望的输入 bundle 摘要。

## 3. 作者 brief（`docs/v2/stage6-author-brief.md`）

brief 随 bundle 冻结，避免不同作者收到不同的口头指示。它规定：

- 作者是全新的隔离上下文，只读 bundle，不运行被评测系统或任何参考程序，不用外部资料；
- 一次只写一个 split，规模与覆盖由 `stage6-holdout-plan.json` 决定（holdout 25 条、每个 scenario 恰好 1 条；DEV / VALIDATION 各 40 条、每个 scenario 至少 1 条；五类最终结论、两个 persona、至少两个 virtual_now、全部必需覆盖项）；
- 编写规则摘要（以领域规格为准，从不写生成编号，不为迁就任何实现调整标签）；
- 用契约检查器自查；
- 输出：仓库外的数据集文件（JSON 数组）与回执；报告只含 split、条数、两个 sha256 与分布，不含内容或位置。

## 4. 数据集回执（`v2-stage6-dataset-receipt/1`）

`eval/v2/stage6_dataset_receipt.py` 只依赖标准库，在导出的 bundle 根目录运行：

1. **核对路径与 bundle**：数据集与回执都在 bundle 之外；严格校验 `bundle-manifest.json`，重新计算摘要并要求它等于带外提供的期望摘要；bundle 文件集合精确，每个文件的 sha256 与字节数一致（§2.4）。任何一处不通过都拒绝。
2. **核对数据集**：UTF-8 JSON 数组；每个 case 通过 `stage6_case_contract.case_errors`；case_id 唯一；`dataset_plan_errors(cases, split)` 为空。任何一处不通过都拒绝，不产生回执。
3. **写出回执**：字段恰好是 `schema`、`split`、`freeze_merge_commit`、`input_bundle_digest`、`input_file_count`、`dataset_sha256`（原始字节）、`case_count`、`scenario_counts`、`archetype_counts`、`final_counts`、`final_status_counts`、`persona_counts`、`distinct_virtual_now`、`contract_validation`、`plan_validation`、`author_context`。`author_context` 是固定的隔离声明（全新隔离上下文、只读冻结 bundle、看不到实现、看不到其他数据集、看不到失败分析、0 次系统运行、0 次 oracle 运行、没有外部资料），作者必须显式 `--attest-isolated` 才会写出；不能如实声明时不得使用。
4. 回执不含路径，不含任何 case 内容。

封存（§5）与 DEV / VALIDATION 入库（§6）都以回执为依据：仓库登记数据集与回执的 sha256，开封 / 入库时逐字段核对。

## 5. Holdout 的编写与封存（下一步，不在本 PR）

1. 人（不是实现会话）在仓库外导出 bundle，核对导出工具输出的摘要等于冻结的仓库清单摘要，然后启动一个全新的隔离作者上下文，只把 bundle 交给它，并告诉它恰好三样东西：split = `holdout`、freeze commit、期望的输入 bundle 摘要。
2. 作者在仓库外写出 holdout 文件与回执，报告只含 sha256 与分布。实现会话**不获知**路径与内容。
3. 实现会话据报告提交封存：`eval/v2/stage6-holdout.manifest.json`（schema `v2-stage6-sealed-holdout-manifest/1`，状态 sealed）记录 freeze commit、输入摘要与文件数、holdout sha256、回执 sha256、条数、scenario / archetype / final / final_status / persona 分布、校验结果与作者声明，以及「隔离是流程与上下文上的隔离，不是文件系统权限」的说明；不含路径或内容。同一 PR 预先提交开封工具 `tools/unseal_v2_stage6_holdout.py`。
4. 开封工具（与 Stage 4/5 的开封工具同样的规程）：工作区干净、封存 manifest 与钉住的值一致、从未开封过；重新核对 27 个冻结输入与摘要；核对回执原始字节的 sha256 与逐字段内容；核对 holdout 原始字节的 sha256、契约与分布；原样复制（不重新序列化）到仓库并再次核对；不提交 commit。两个文件的位置由人在开封时提供，工具本身不知道、不搜索、不推导它们的位置。
5. **只在 Stage 6 结束时开封一次**，与 oracle 路径一起运行并报告；开封之后不调参。

## 6. DEV 与 VALIDATION（封存之后）

- 由隔离作者使用同一个冻结 bundle 编写（可以是两个不同的隔离上下文），各自产生回执。数据集与回执入库为 `eval/v2/stage6-dev.json` 与 `eval/v2/stage6-validation.json` 及其作者回执；入库前用回执核对 sha256 与分布。
- 入库后由实现会话运行 oracle / reference fixture 路径。oracle 与标签不一致时，按冻结规格裁决：标签违反规格则修正标签（记录理由与修订，原文件的 sha256 保留在 HANDOFF）；实现违反规格则修正实现（按契约违例记录）。不得为了迁就实现而改标签，也不得为了迁就标签而改规格之外的实现。
- oracle 检查通过之后，Guard / Gateway / 渲染器 / 评分器冻结为 annotated tag `v2-stage6-action-core`（§22 Stage 6.4 的退出条件）。之后才进入 Stage 6.5 的 DEV 迭代。

## 7. 实现会话的约束

- 实现会话（读过任何实现代码、设计、测试或开发记录的上下文）**不编写** holdout、DEV 或 VALIDATION 的 case，不替作者「补全」或「修正」case 内容，不获知 holdout 的路径或内容，不运行开封工具（只有在 Stage 6 结束时由人明确要求才运行）。
- 实现会话的测试可以使用评测器 fixture（`tests/stage6_eval_support.py`），但它们不是数据集，从不写入数据集文件，也不满足完整的分布约束。
- 隔离是流程与上下文上的隔离，不是文件系统权限：哈希证明的是不可变，不证明文件从未可读。

## 8. 本 PR 的验收

- 输入 manifest 与仓库当前文件逐字节一致、可重复生成；允许清单恰好 27 个文件；没有实现、设计、测试、开发记录或任何数据集；只有三个标准库检查器是代码。
- 导出的 bundle 自足：在 `-I -S`（没有仓库、没有 site-packages）下运行契约检查器与回执工具，不加载任何非标准库模块；契约的词表一致性检查为空；回执工具能核对 bundle。
- 回执工具 fail-closed：没有隔离声明、期望摘要缺失或格式不对、bundle 清单自证（删 / 增 / 重算哈希后重算摘要）、期望摘要不符、文件集合不精确（额外文件、嵌套的实现文件、`__pycache__`）、清单字段畸形、字节数不符、数据集或回执在 bundle 之内、数据集无效、case_id 重复或不满足分布约束时都拒绝且不写文件；回执工具自身运行不会在 bundle 中产生任何文件。
- Stage 4/5 的 17 个冻结作者输入与其摘要 `7b3d4684…` 不变。
- 仓库中仍然没有任何 Stage 6 数据集、私有 holdout、封存 manifest 或开封工具，没有任何正式 LLM 运行。
