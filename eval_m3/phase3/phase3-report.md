# M3 Phase 3 运行时交付记录

Phase 2 已提交、推送并通过 [PR #43](https://github.com/sjhesjaj/GroundedAgent/pull/43) 合并，main 为 `94961f21cf4f52c13aed99823cd85502a4793855`。本 worktree 的分支 `m3-phase3` 从该提交创建。方案第 9 版已写入用户确认的历史复用回合口径。本阶段基本运行时已实现，默认仍为 `stage6`；Phase 3 已提交并推送 `m3-phase3`、开 PR 待审阅，尚未合并。

## 实现

| 范围 | 行为与实现位置 |
|---|---|
| 检索 | `aftersales_service/knowledge_base.py` 按业务时钟过滤生效期；hybrid 逐段 cosine ≥ 0.45 后沿用 RRF；BM25 退回先要求整次最高分 ≥ 2，再取原正分排序。产品 trace 保留模式、退回原因、信号、粒度、阈值、最高分及筛除数。 |
| 会话策略 | `persistence.py` 的 schema 2 明确保存创建策略；`service.py` / `conversation.py` 在缓存和重启加载、消息、GET、操作员审批路径检查，不一致返回 409。未知旧 manifest 不猜测或自动迁移。 |
| 待审批查询 | `pending_requests.py` 与 `Conversation._pending_requests` 只读本会话 gateway 的 `WAITING_APPROVAL`。普通 Evidence 不产生 grounding records。生成只用最新查询；EMPTY 是可引用的当轮空列表，ERROR 没有待审批来源，不能据空列表断言某单已执行。 |
| 话术 | `customer_wording.py` 固定 m3 拒答、转人工、边界、问候、致谢、告别；完整寒暄在创建 provider 前处理，不启动 run。确认词、混合业务、暂停追问不拦截。 |
| 调用控制 | 决策 max_tokens 512、生成 1024；有限正数 provider 超时，默认 180 秒。trace 逐调用记录 token、限制、超时、耗时；模型调用失败的回合保持原子回滚，错误 trace 仍保留已发生的调用。Gateway 提交后的故障仍按 M2 marker 协议恢复。 |
| 前端 | `App.vue`、`api.js` 处理策略 409 并提示新建会话；`AgentDetails.vue` 显示 KB 标题、版本、段落、doc_id 和每次决策/生成用量。 |

控制循环、40 条消息 / 2,000 字限制、确定性 Guard、M1 grounding、审批、ActionGateway 幂等和 M2 恢复协议继续保留。没有修改 Stage 6 domain / generation / registry；恢复仍只处理原 gateway 动作，不调用模型。

## DEV 校准与冻结

只读冻结 KB-DEV 的 40 case / 50 原始用户回合，用本机真实 `bge-m3` 查询向量选择候选。用户确认 hybrid 逐段 ≥ 0.45，BM25 整次 max ≥ 2。新门槛没有额外删除原 top4 的精确 gold 命中，静态命中仍为 19 / 35 段；两条无关问题被过滤。这不是答案准确率。相似主题但缺所需事实的提问仍可能匹配，是否能答由决策和事实评测处理；本阶段没有改 RRF、rerank、top_k。详见 [校准记录](dev-retrieval-calibration.md) 和 JSON。

35 篇语料及 `knowledge_base/README.md` 共 36 个 tracked 文件与 main 规范化内容一致；`knowledge_base/`、`policy_sources/`、`aftersales/`、`eval_v2/`、`eval/v2/`、`system_fixtures/`、golden fixtures 和 Phase 2 datasets/spec/sealed 均零 diff。DEV 和 24 ID 清单的 UTF-8/LF hash 匹配冻结 manifest；CRLF 检出的 raw hash 不作为内容改动。

HOLDOUT 仍为 `eval_m3/sealed/kb-holdout.zip`，23,711 字节，本地只读，SHA256 `4e6a705d86bf381825ad63bc347be6a15b792bd4e2d4831df971ea1a32ab3f63`。本阶段仅 hash 原始 ZIP 字节，没有列出成员、解压或读取内容。完整边界证据见 [frozen-boundaries.json](reports/frozen-boundaries.json)。Phase 2 的两个集合数量、类型分布、重叠结果和封存方法见 [Phase 2 记录](../reports/phase2-report.md)：DEV 40 / HOLDOUT 20；最大问题-gold 重叠 0.448276 / 0.238806，≥0.80 与 ≥0.85 均 0。

## 真实 DeepSeek 抽查（手工抽查，非正式评测）

以下是手写对话的人工观察，用来确认真实模型下产品路径能走通，不是 KB-DEV / HOLDOUT 评测，也不是里程碑成绩；正式评测在 Phase 4 runner 完成后的 Phase 5 进行。本地真实 DeepSeek `deepseek-flash`，2026-10-09 18:21 运行一次，4 段手写对话 / 10 回合，每段独立临时 fixture DB，没有操作员批准，没有读取 DEV / HOLDOUT 做模型评测。

| 对话 | 观察 |
|---|---|
| 错发商品运费与凭证追问 | 能回答先垫付、凭证、审核通过后退款返还、最高 20 元；追问重新读 KB，有 doc_id / version / section 引用。 |
| 内衣退货 → 重启 → “进度怎么样？” | 先读订单，申请进入 WAITING_APPROVAL；重启没有模型调用；追问调用 get_my_pending_requests 并引用当前结果，正确回答待人工审批，没有把历史 AS-1001 已完成换货当作本次进度。 |
| 非质量运费 → T 恤退货 → “现在进度怎么样？” | 政策和动作均接上；进度再次读本会话待审批工具并正确回答待人工审批。 |
| 你好 / 谢谢 / 再见 | 使用固定模板，provider 工厂和模型调用均为 0。 |

19 次实际模型调用，共输入 75,729、输出 1,309 tokens；产品 trace 的调用数、每次 token、max_tokens 和超时与 provider 记录逐项一致，没有重复记录。7 个业务回合耗时 min / median / max 为 2.489 / 3.732 / 7.962 秒；仅为本次手工样本，不报告 pass^3、泛化能力或账单费用。原始记录见 [deepseek-spot-check.json](reports/deepseek-spot-check.json)，人工复核与计数见 [spot-check-review.json](reports/spot-check-review.json)。

一条 T 恤进度回复复用了历史商品名称、规格和数量；本轮 pending 工具支持订单、商品 ID 和状态，没有商品标签字段。这些历史事实正确，但须按第 9 版在后续评测中单列历史复用标记；本阶段未据此调 prompt，也不把它单独判为失败。

## 验证

前端 `node --test tests/*.test.js` **39 / 39**，`npm run build` exit 0。浏览器通过真实路由和脚本模型验证引用展开及用量展示；策略切换返回 409 后保留旧对话、禁用输入并提示新建会话。移动视口 DOM 宽 375、scrollWidth 375，无横向溢出。截图见 [桌面引用](reports/ui-citations-desktop.png) 和 [移动引用与 409 提示](reports/ui-policy-mismatch.png)。临时 UI server 已停止，未使用真实持久业务库。

独立代码复核发现并修正两项问题：同 run 的旧 pending 状态能在最新 EMPTY / ERROR 后继续成为答案来源；工具层相关性统计漏进产品 trace。修正后的 4 条 pending 来源回归及产品 trace 检查 **5 / 5** 通过，未发现派生事实中的旧状态侧通道。新增 runtime 19、generation 20、retrieval 14，共 53 项离线测试；3 个既有 synthetic 检索查询仅增加已有主题词以实际命中新门槛下的目标段，安全断言及阈值未变。

首次全量离线 **2,899 项，1 项失败**：旧 m3 scripted equivalence 场景把新增 generation trace 当成决策 trace，读取不存在的 control_step。原失败报告保存在 [offline-results-attempt1.json](reports/offline-results-attempt1.json)。等价测试已通过：45 个原场景自身断言运行成功；比较真实 m3 payload，仅排除 model_calls（包括错误响应中的调用监控）、严格映射 3 种批准的固定话术，以及第 9 版早已规定的唯一 provider 不可用探针输入差异（m3“帮我查询订单”，Stage 6“你好”）。其余 44 个场景不改输入，业务字段、grounding 和回滚断言继续比较；Stage 6 错误形状仍严格断言，冻结 golden 和原 recorder 不改。

修正后的复跑（18:36–18:40，最后一次代码改动在 18:33 之后）**2,899 / 2,899** 通过，用时 274.8 秒，但结果没有写进本记录，原 agent 即因额度中断。

## 交接后复核

接手时工作树停在 main `94961f2`，20 个已修改文件和 7 个未跟踪项全部未提交；没有残留测试进程，原 agent 18:03 启动的 `ollama serve` 仍在运行，已停止（离线验证本来就拦截网络，不依赖它）。先把现场原样提交为 WIP 快照，再在同一 worktree 复跑：

| 检查 | 命令 | 结果 |
|---|---|---|
| 全量离线 | `.venv/Scripts/python.exe -X utf8 tools/verify_m3_offline.py --output …` | exit 0；发现 2,901，排除 2 项真实 DeepSeek live，运行 **2,899 / 2,899**，0 失败 / 0 错误 / 0 跳过，323.9 秒；未 mock 的 HTTP、socket、真实持久库访问均为 0，M3 embedding 缓存 242 项未变。见 [offline-results.json](reports/offline-results.json)。 |
| 前端测试 | `node --test tests/*.test.js` | exit 0，**39 / 39** |
| 前端构建 | `npm run build` | exit 0 |
| 冻结边界 | `git diff 94961f2` 限定 `aftersales/`、`eval_v2/`、`eval/v2/`、`policy_sources/`、`knowledge_base/`、`system_fixtures/`、`tests/fixtures/`（含 M2 golden）、`eval_m3/datasets|spec|sealed` | 已提交与工作树均零 diff，无未跟踪文件 |
| HOLDOUT | 仅对原始 ZIP 字节做 SHA256 | `4e6a705d86bf381825ad63bc347be6a15b792bd4e2d4831df971ea1a32ab3f63`，23,711 字节，与封存一致；未列出、未解压、未读取 |

复跑无失败，交接后没有修改任何代码或测试，只补完本记录并更新离线结果 JSON。

## 交接与边界

本阶段没有实现 eval_m3 runner，没有运行 Phase 5 DEV / HOLDOUT 成绩评测，没有切换默认策略。没有待用户决定的方向；门槛粒度已经用户确认。

留给后续阶段的事项：

- T 恤进度回复复用历史商品标签一事，按第 9 版在 Phase 4/5 的“仅凭历史作答比例”中计数，本阶段不调 prompt。
- 相关性下限只用 KB-DEV 校准；能否作答仍由决策与事实评测衡量，正式数字在 Phase 5。
- 默认策略保持 `stage6`，Phase 5 结果出来后再切换。

交付：分支 `m3-phase3`（基于 main `94961f2`）已推送并开 PR，待审阅，未合并。改动 38 个文件：`aftersales_service/` 9 个修改 + 2 个新增（`customer_wording.py`、`pending_requests.py`），前端 3 个修改 + 1 个新增测试，测试 6 个修改 + 3 个新增，`README.md`、方案第 9 版状态行，以及本目录的校准脚本、抽查脚本、报告和证据。
