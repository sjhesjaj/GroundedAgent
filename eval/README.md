# eval/ — 评测结果管理规则

## 进 Git 的（小、需要复现和对比）

| 内容 | 位置 | 说明 |
|---|---|---|
| 基线 / 回归 / 任一带标签的运行 | `eval/<label>.json` | 环境（commit、模型 digest、量化、Ollama 版本、Wiki build）、检索配置、system prompt 哈希、请求形态、每次运行的 summary、aggregate、gates，以及**每个 case 每次运行的结果**（用于翻转分析） |
| 对比结论 | `eval/stage0_comparison.json`（以及以后的同类文件） | `compare_stage0.py` 的输出 |
| 冒烟 / 真实调用记录 | `eval/deepseek_smoke.json` | 逻辑与实际 prompt 的差异、token、耗时；不含请求头和 Key |
| Trace 开销 A/B | `eval/stage1_trace_overhead.json`（Stage 1）、`eval/pmi_trace_overhead.json`（合入 main 之后） | `trace_overhead.py --output <file>` 的结果（TRACE on/off 各 200 次 mock 请求）；已存在的文件不会被覆盖，结果里记录 git commit 和工作区是否干净 |
| 模型对照 | `eval/stage25/`、`eval/post_main_integration/` | `model_comparison.py --series stage25\|pmi`。每个 series 用自己的 label 前缀（`stage25_*`、`pmi_*`）和目录，已有报告不会被重写 |
| 诊断标签 overlay | `eval/diagnostic_labels/<dataset>.labels.json` | Stage 2 起。人工编写，锁定数据集 sha256 和它所针对的 wiki 语料；冻结数据集本身不改 |
| 诊断报告 | `eval/diagnostics/<label>.diagnostic.json` / `.md` | `python -m diagnostic_eval --eval eval/<label>.json --labels <overlay>` 的输出：case 级阶段诊断，以及 root cause 分布 |
| 脚本 | `eval/*.py` | harness、对比、冒烟、开销基准 |

每个 case 的每次运行在 `eval/<label>.json` 里都带有 `trace_run_id`。某个 case 失败时，用这个 id 去对应 label 的 Trace 库里查：

```powershell
.\.venv\Scripts\python.exe -m agent_trace --db eval\artifacts\<label>\traces.sqlite show <trace_run_id>
```

## 不进 Git 的（大、原始、可再生成）

`eval/artifacts/`，已在 `.gitignore` 中忽略：

- `eval/artifacts/<label>/run-N.json`、`summary.json`、`summary.md`：`evaluate_answerability.py` 的原始逐次输出。`run_stage0_eval.py` 会写到这里，再把需要留档的部分汇总进 `eval/<label>.json`
- 控制台日志：用 `> eval\artifacts\<label>.console.txt` 重定向到这里
- `eval/artifacts/<label>/traces.sqlite`：Stage 1 起，这次评测所有 case 的 Trace。Eval 的 Trace 不截断，保留完整的 prompt、证据和工具结果，所以体积较大；它可以再生成，因此不提交

## 历史例外

Stage 0（commit `416fd0d`）的原始输出在这条规则制定之前就已经提交，位置是 `eval/runs/baseline_qwen/`、`eval/runs/regression_qwen/`、`eval/*.console.txt`。**它们保留在原处，不移动也不改写**，因为 `stage0_comparison.json` 和 HANDOFF 都引用了这些路径。新的运行不要再往 `eval/runs/` 里写。

## V1 数据集的位置

不被任何冻结记录引用的 V1 数据集放在 `eval/v1/datasets/`：`eval_cases.json`、`eval_cases_extended.json`、`eval_agent_routes.json`、`eval_no_answer.json`、`eval_temporal_freshness_dev.json`。脚本里的默认路径已经指向新位置；历史结果文件（例如 `eval/stage3/*.json`）里记录的是当时的文件名，保持原样。

下面这些**留在仓库根目录，不要搬**，因为冻结记录或校验代码按根目录路径引用它们：

| 数据集 | 锁定它的地方 |
|---|---|
| `eval_answerability_validation_v1.json` | `eval/environments/eval-env-v1/manifest.json`，`eval_env` 按根目录路径校验 sha256 |
| `eval_answerability_blind_v2.json`、`eval_orchestrated_routes_blind_v2.json` | `evaluation_runs/blind-v2-freeze-manifest.json` |
| `eval_orchestrated_routes_dev.json`、`eval_orchestrated_routes_validation_v1.json` | `eval/label_revisions/` 的冻结 overlay 按文件名关联 |
| `eval_answerability_dev.json`、`eval_answerability_holdout.json`、`eval_orchestrated_routes_holdout.json` | `evaluation_runs/dataset-sha256.json` 的历史哈希记录 |
| `eval_temporal_freshness_holdout.json` | `eval/stage3_freshness_holdout.py` 要求自封存提交 `7cc5abe` 起没有任何提交动过这个路径 |
