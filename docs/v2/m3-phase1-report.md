# M3 Phase 1 语料删减、用户确认与冻结

状态：**已审阅，已冻结**。冻结在本 PR 合入 main 后生效，之后修改 knowledge_base/ 语料需用户同意。

## 本轮结果

用户已审阅全部 35 篇，并确认全部自拟服务口径。本轮删除 151 处元说明片段，让正文保留客服服务条款和操作说明；不改事实、数字、活动日期、条件、品类前提或 front matter。两篇旧版标题中的“2025 年存档旧版”保留，正文的过期自声明删去，effective_to 不变。

删后为 **35 篇 / 95 段**：规则复述 12、知识库独有 23；faq 20、guide 13、promotion 2。固定业务时间 2026-11-15T10:00:00+08:00 下，33 篇在期、2 篇存档已失效。

正文去标题、去空白并计入数字和标点为 **59–316 字**。13 篇低于 150 字，用户明确批准“这些篇允许短于 150 字，保留现有事实直接冻结”。普通构建门禁为 150–800 字；knowledge_base/reviewed-short-bodies.json 逐篇绑定 UTF-8/LF 规范化全文 SHA256，内容变更即失去例外。未补字，未新增政策事实。

逐篇字数、版本、生效区间和 hashes 见 [m3-corpus-build.json](m3-corpus-build.json)，用户审阅与短文决定见 [m3-corpus-review.md](m3-corpus-review.md)。规范化 hash 用于跨 Windows CRLF / LF checkout 验证；raw_sha256 对应本次工作树文件字节。

## 实现与边界

此 PR 包含原有未提交的 Phase 1 loader、heading-aware chunker、索引及 embedding 缓存实现；本轮为删减稿调整正文计数，并加入内容绑定的短文例外。正式构建校验 JSON front matter、有效期、资格 lint、规则复述数字及单位。缓存 key 为模型名和精确嵌入输入 SHA256；离线 cache miss 不调用 provider、不写缓存。

真实 bge-m3 段落向量为 1024 维，95 段严格只读复建成功。本机忽略目录 .cache/m3-embeddings 共 188 个缓存条目，含删减前仍保留的历史内容缓存；当前构建只使用 95 段对应条目。缓存不随 Git 分发，其他机器先在线填充，再离线检查。

默认仍为 stage6；aftersales/、eval_v2/、eval/v2/、policy_sources/、冻结规则包及 golden fixture 零 diff。未编写评测集、检索调参、实现评测 runner 或 get_my_pending_requests。第 8 版方案中的 Phase 3 新工具和历史复用指标保持其阶段边界。

## 验证

| 检查 | 结果 |
|---|---|
| 全量离线 unittest | 2829/2829 OK；failures/errors/skips 均 0；306.315 s；exit 0 |
| 构建 / 缓存 / 内容绑定短文例外 unittest | 43/43 OK；exit 0 |
| 真实 bge-m3 在线建缓存、只读离线复建 | 各 exit 0；35 篇 / 95 段 / 1024 维 |
| 资格 lint、复述数字 / 单位、生效期及批准后的长度门禁 | 35/35 通过 |
| Stage 6 golden / m3 scripted equivalence | 全量中通过；45 个 golden 场景，m3 45/45，仅排除 trace.model_calls |
| 前端 API | 20/20 OK；exit 0 |
| 冻结目录及 fixture / whitespace | 零 diff；git diff --check exit 0 |

全量由 tools/verify_m3_offline.py 隔离运行：只排除原两项真实 DeepSeek 测试，临时 V1 数据库，真实 HTTP/socket 和仓库持久数据库连接被拦截；缓存字节及 mtime 保持不变。已吸收 main 的 PR #40 测试隔离修复，本次未 mock HTTP、真实 socket 及仓库持久业务数据库连接尝试均为 0。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pip check
.\.venv\Scripts\python.exe -X utf8 -m aftersales_service.build_knowledge_base --cache-dir .cache/m3-embeddings
.\.venv\Scripts\python.exe -X utf8 -m aftersales_service.build_knowledge_base --cache-dir .cache/m3-embeddings --offline
.\.venv\Scripts\python.exe -X utf8 tools/verify_m3_offline.py --output docs/v2/m3-phase1-offline-results.json
node --test frontend/tests/api.test.js
git diff --check
```

Phase 1 从已合并的 PR #39 / main 74ab12b 起步，提交前吸收 PR #40 / main 3e40c15 并重新运行最终全量检查。按用户本轮授权提交并推送 m3-phase1，开 PR 并合并到 main；合并后的 main 是 Phase 2 的起点。
