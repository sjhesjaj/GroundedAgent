# M3 Phase 2 交付记录

语料 PR [#41](https://github.com/sjhesjaj/GroundedAgent/pull/41) 已合并，冻结提交 `4e1c3a5fd36247b3c4ae1f862dd7ef7ddd9ae259`。35 篇文档已删元说明，事实及 front matter 保持；用户明确批准 13 篇短正文直接冻结。lint、复述数字、生效期、真实 bge-m3 embedding 缓存及只读离线构建通过。Phase 1 合并前全量离线 2829 / 2829、前端 20 / 20 通过。

从该提交建立本地 m3-phase2，评测内容及 Phase 2 代码已可审阅；用户已确认历史复用按回合计，并授权 m3-phase2 提交、推送、开 PR 和合并。本记录成于该提交之前。

## 集合

DEV 40 case / 50 回合；HOLDOUT 20 case / 25 回合。工具路由、gold source/doc_id/version/sections、must_include/must_not_include、期望处置均逐回合标注；追问标明 allow_history_only。动作参数只使用冻结合约。

| type | DEV | HOLDOUT |
|---|---:|---:|
| policy_single | 6 | 3 |
| policy_multi | 4 | 2 |
| expired_version | 2 | 1 |
| promotion | 2 | 1 |
| unanswerable | 2 | 1 |
| action | 4 | 2 |
| mixed | 4 | 2 |
| followup | 6 | 3 |
| smalltalk | 4 | 2 |
| off_topic | 2 | 1 |
| injection | 4 | 2 |
| 合计 | 40 | 20 |

DEV 完成主代理与独立语义复核，并通过无问题内容的静态 schema 检查（dev-schema-validation.json）。HOLDOUT 在 fork_turns=none 子任务中独立写作和自检，未看 DEV；主代理只核验封存完整性，没有读取内容。

## 重叠与封存

采用用户确定的 max(规范化 SequenceMatcher, 二元字符 Jaccard)，逐回合比较完整引用标题段正文。≥0.85 阻断并改写；≥0.80 单独报告。

| 集合 | gold 段落比较 | 最大值 | ≥0.80 | ≥0.85 | 空 gold 回合 |
|---|---:|---:|---:|---:|---:|
| DEV（主代理复跑） | 46 | 0.4482758621 | 0 | 0 | 23 |
| HOLDOUT（作者封存前） | 27 | 0.2388059701 | 0 | 0 | 12 |

HOLDOUT 路径 `eval_m3/sealed/kb-holdout.zip`，23711 字节，SHA256 `4e6a705d86bf381825ad63bc347be6a15b792bd4e2d4831df971ea1a32ab3f63`。内部 JSON SHA256（作者封存前记录）为 `ed3077962fa08b9f3af518f519ba635c7e1c2be3df85063b15528efc22e08de5`。完整 JSON、逐对报告及含问题的编写脚本已归档，作者清理三个明文文件。ZIP 现为本地只读；主代理仅 hash 原始字节，未列出成员、解压或打开；Phase 5 前禁止开封。详见 kb-holdout-seal.json 与 holdout-integrity.json。

Stage 6 DEV 清单恰好 24 个 ID：001、004–017、019–025、039、040。每条记录自身时钟及初始状态 canonical JSON SHA256，源 Git blob 与 UTF-8/LF hash 已保留，主代理读回 24 / 24 核验通过。

## 验证与边界

- `python -m unittest tests.test_m3_evaluation_overlap`：exit 0，17 / 17。覆盖边界阈值、空 gold、精确版本/标题、逐回合/逐段比较、指定集合隔离及旧 CLI 分支。
- `python check_evaluation_overlap.py --m3-dataset eval_m3/datasets/kb-dev.json --corpus-dir knowledge_base --policy-dir policy_sources --report eval_m3/reports/kb-dev-overlap.json`：exit 0；上表 DEV 汇总。
- `python tools/verify_m3_offline.py --output eval_m3/reports/phase2-offline-results.json`：exit 0，2846 / 2846，失败/错误/跳过均 0；仅排除两项真实 DeepSeek live 测试。真实 HTTP/socket/持久业务库尝试 0，188 项 embedding cache hash/mtime 不变。
- `git diff --check`：exit 0。35 / 35 frozen corpus normalized hashes 一致；knowledge_base、policy_sources、aftersales、eval_v2、eval/v2、system_fixtures 零 diff 且无新增文件。
- 没有执行模型评测、eval_m3 runner、检索调参、真实动作或实现 get_my_pending_requests。本阶段只有数据、规格及离线重叠检查工具。

规则一致性天数仅抽取退/换货窗口；价保 7 自然日、退款工作日、运费险时长及一般经过时长均排除。品类前提仍由普通事实断言验证。

## 用户已确认的历史复用指标

方案已更新为 Revision 9。“仅凭历史作答比例”按追问回合计：分母为复用了可核验历史事实的追问回合；分子为其中至少一项复用事实缺少本轮读取支持的回合。同一回合只计一次，同时列出事实及读取证据；分母 0 为 N/A。正确历史复用本身不造成 E2E 失败，错误事实、无效引用及动作安全仍按原标准检查。该选择没有开封 HOLDOUT，也没有修改冻结语料。Phase 2 无待决方向。

## 文件与 Git 状态

新增 eval_m3/datasets/kb-dev.json、sealed ZIP 与回执、spec/ 的评测规格/数据 hash 清单/24 ID 清单、reports/ 的静态验证与离线测试记录，以及 tests/test_m3_evaluation_overlap.py。修改的既有文件为方案 docs/v2/m3-policy-rag.md（Revision 9）及 check_evaluation_overlap.py：增加显式 M3 问题对 gold 段落模式，保留旧 CLI 算法及阈值；import 时不读取旧集合。

tracked diff（不含未跟踪的新文件）：

```text
check_evaluation_overlap.py | 211 +++++++++++++++++++++++++++++++++++++++++++-
 1 file changed, 209 insertions(+), 2 deletions(-)
```

```text
M check_evaluation_overlap.py
?? eval_m3/datasets/
?? eval_m3/reports/
?? eval_m3/sealed/
?? eval_m3/spec/
?? tests/test_m3_evaluation_overlap.py
```

上述 Git 状态为提交前快照；用户已授权本阶段提交、推送、开 PR 和合并。
