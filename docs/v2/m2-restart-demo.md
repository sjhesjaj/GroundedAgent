# M2 重启演示（Windows，手动）

目的：在真实的后端进程上演示 M2 的重启恢复。先把一个会话推进到「待审批」，再把另一个会话停在「追问」，然后用 `taskkill /F` 强制结束后端（不走正常关闭流程），重启后刷新页面，确认两个会话都还在：待审批单可以批准，追问可以接着回答，批准后回执只有 1 张。

这个演示在两次请求之间结束进程，验证的是"重启后会话和业务状态都还在"。在一次请求的中途杀掉进程（例如写入完成、会话还没保存时），这种情况由 `tests/test_aftersales_crash.py` 的 9 个子进程测试覆盖，手动很难卡准时间点。

已在 2026-10-08 用真实后端进程、真实 DeepSeek 跑通，7 项检查全部通过，结果见 `docs/v2/m2-session-recovery.md` 的 Results 一节。

## 0. 准备（只需做一次）

所有命令都在 M2 worktree `knowledge-agent-m2` 下运行（分支 `codex/m2-session-recovery`，它有自己的 `.venv`，已装好 LangGraph）。

```powershell
cd C:\Users\h000_\Documents\ChatGPT\agent项目改进\knowledge-agent-m2

# M2 worktree 里没有 .env：从主 checkout 复制一份（里面是 DeepSeek 配置，不入库）
Copy-Item ..\knowledge-agent\.env .env

# 可选：从干净的演示数据开始（只删除本目录下的演示数据目录）
Remove-Item -Recurse -Force .aftersales-demo -ErrorAction SilentlyContinue
```

**模型**：主 checkout 的 `.env` 默认 `LLM_PROVIDER=ollama`，售后 Agent 需要 DeepSeek。后端窗口启动前先设置环境变量。进程环境变量优先于 `.env`；DeepSeek 的 API key 仍然只从 `.env` 读取。

```powershell
# 「后端窗口」，在 knowledge-agent-m2 目录下
$env:LLM_PROVIDER = 'deepseek'
.\.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000
```

- 预期：出现 `Uvicorn running on http://127.0.0.1:8000`。第一次请求或启动时，会在当前目录生成 `.aftersales-demo\`（已被 git 忽略）。
- **不要用 `start_api.ps1`**：它带 `--reload`，会多起一个监控进程，`taskkill` 结束的就不一定是真正处理请求的进程。

**前端**：用 M2 worktree 自己的前端。它会记住会话，刷新页面后能恢复；主 checkout 的前端没有这个改动。

```powershell
# 「前端窗口」
cd C:\Users\h000_\Documents\ChatGPT\agent项目改进\knowledge-agent-m2\frontend
corepack pnpm install --frozen-lockfile   # 本机已有 pnpm 缓存时可加 --offline；node_modules 不入库
corepack pnpm dev                         # 页面 http://127.0.0.1:5173，/api 代理到 8000
```

**一键版本**：`.aftersales-demo\restart-demo\run_restart_demo.bat` 会用真实后端进程和 DeepSeek 跑完下面整个流程（不经过浏览器），并把报告写到 `.aftersales-demo\restart-demo-<时间>\report.txt`。这个脚本只在本机，不入库，只作说明；下面的手动步骤照常可用。

### 核对脚本（第 1、3、4 步都会用到）

另开一个「核对窗口」（PowerShell，在 `knowledge-agent-m2` 目录下）。这段脚本从服务端读出每个会话的状态，并统计演示数据库里的三张业务表：

```powershell
$data = if ($env:AFTERSALES_DATA_DIR) { $env:AFTERSALES_DATA_DIR } else { Join-Path $PWD '.aftersales-demo' }
$gen = (Get-Content -Raw (Join-Path $data 'generation.json') | ConvertFrom-Json).generation
Get-ChildItem (Join-Path $data "gen-$gen\sessions") -Filter *.json | ForEach-Object {
    $file = Get-Content -Raw $_.FullName | ConvertFrom-Json
    $view = Invoke-RestMethod "http://127.0.0.1:8000/api/aftersales/sessions/$($_.BaseName)"
    [pscustomobject]@{
        session  = $_.BaseName
        status   = $view.status
        messages = @($view.messages).Count
        pending  = (@($view.pending_actions) | ForEach-Object { "$($_.pending_action_id) $($_.status) approval_recorded=$($_.approval_recorded)" }) -join '; '
        inflight = [bool]$file.inflight
    }
} | Format-Table -AutoSize -Wrap
.\.venv\Scripts\python.exe -c "import json,sqlite3,pathlib,os; d=pathlib.Path(os.environ.get('AFTERSALES_DATA_DIR') or '.aftersales-demo'); g=json.loads((d/'generation.json').read_text())['generation']; c=sqlite3.connect(d/('gen-'+g)/'aftersales-demo.db'); print({t: c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('pending_actions','action_receipts','after_sales_cases')})"
```

表里可能多出几条 `OPEN`、`messages = 0` 的空会话（例如之前加载页面时新建的），可以忽略。

## 1. 制造两个会话

1. 浏览器打开 `http://127.0.0.1:5173`（**标签页 A**），发送：`我要退 ORD-1001 里的内衣，不想要了`
   - 预期：回复是一张退货动作卡片，状态 `WAITING_APPROVAL`（等待审批），卡片上有「批准」「拒绝」按钮。
2. 新开一个**标签页 B**，打开 `http://127.0.0.1:5173`，点左侧「新建会话」，然后发送：`我想退货，帮我办一下`
   - 新标签页会先恢复浏览器里最近的会话（也就是 A），所以要点「新建会话」，B 才是独立的会话。
   - 预期：回复是追问，例如"为了继续处理，请告诉我：您要办理的订单号；订单里具体是哪一件商品；申请的原因……"，会话状态 `NEEDS_CLARIFICATION`；展开 Agent Trace，能看到 `Run 1 / Step 1`。
   - 如果模型没有追问，在 B 里再点一次「新建会话」，重发同一句。
3. 在核对窗口运行核对脚本。
   - 预期：有一个会话是 `WAITING_APPROVAL`、pending 一列是 `PA-… WAITING_APPROVAL approval_recorded=False`；有一个会话是 `NEEDS_CLARIFICATION`；所有会话的 `inflight` 都是 `False`。
   - 数据库计数：`{'pending_actions': 1, 'action_receipts': 0, 'after_sales_cases': 2}`。

## 2. 强制结束后端

在核对窗口运行：

```powershell
$apiPid = (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess
taskkill /F /PID $apiPid
```

- 预期：`成功: 已终止 PID 为 … 的进程。`（英文系统是 `SUCCESS: The process with PID … has been terminated.`）。
- 后端窗口直接回到命令提示符，**没有** `Shutting down`、`Application shutdown complete` 这类正常关闭的日志。

## 3. 重启，刷新页面，两个会话都还在

1. 在后端窗口用同样的命令重新启动（`$env:LLM_PROVIDER` 在这个窗口里仍然有效）：

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000
   ```

   - 预期：正常启动。启动时会扫描带 in-flight marker 的会话；这次是在两次请求之间结束的，没有需要恢复的。
2. 在核对窗口再运行一次核对脚本。
   - 预期：两个会话的 `status`、`messages`、`pending` 和第 1 步**完全相同**，`inflight` 都是 `False`，数据库计数也和第 1 步相同。
3. **刷新标签页 A 和标签页 B**（F5）。
   - 预期：A 显示原来的对话和退货卡片（`WAITING_APPROVAL`），B 显示原来的追问（`NEEDS_CLARIFICATION`）。每个标签页记得自己的会话；重启前已有的 Agent Trace 细节不会恢复，只有对话记录。
4. **标签页 A**：点卡片上的「批准」。
   - 预期：卡片变为 `EXECUTED`，出现回执号 `RC-…` 和新的售后单号，会话状态变为 `OPEN`。
5. **标签页 B**：回答追问，例如：`ORD-1001 里那件 T 恤，尺码不合适，想退`
   - 预期：回复不再是追问；这一轮的 Agent Trace 从 `Run 1 / Step 2` 开始，说明还是原来的 run，并且接着用剩下的步数（重启之前追问占用了 Step 1）。
   - 通常模型会再提交一张退货单，进入 `WAITING_APPROVAL`；具体结果取决于模型。**不要批准这张单**，否则第 4 步的回执数会变成 2。

> **备选：不刷新、直接操作。** 也可以跳过第 3.3 步，不刷新页面，直接在原标签页里批准或回答。前端带着原来的会话 ID 去请求重启后的后端；如果后端没有恢复这个会话，会显示 `session_not_found` 错误。
>
> 会话是怎么被记住的：会话 ID 存在 `sessionStorage`（每个标签页各一份，刷新后还在）和 `localStorage`（浏览器里最近的会话，新开标签页时使用）。服务端已经没有这个会话（例如重置了 Demo）时，页面会清掉记录并新建会话；浏览器禁止存储时，页面每次加载都新建会话，和以前一样。

## 4. 批准后回执只有 1 张

1. 在核对窗口再运行一次核对脚本。
   - 预期：A 的会话 `OPEN`，它的 pending 是 `PA-… EXECUTED`；数据库计数为 `'action_receipts': 1`、`'after_sales_cases': 3`。`pending_actions` 为 1；如果 B 又提交了一张退货单，则为 2。
2. 可选：对 A 的同一张单再批准一次（把下面的两个占位符换成核对脚本里 A 的会话 ID 和 `PA-…`）：

   ```powershell
   $body = @{ pending_action_id = 'PA-替换为A的单号'; decision = 'APPROVE' } | ConvertTo-Json
   Invoke-RestMethod -Method Post -ContentType 'application/json' -Body $body `
       "http://127.0.0.1:8000/api/aftersales/operator/sessions/替换为A的会话ID/decision" |
       Select-Object -ExpandProperty action | Select-Object status, idempotent_replay
   ```

   - 预期：`status = EXECUTED`、`idempotent_replay = True`；再运行核对脚本，`action_receipts` 仍是 1。

## 结果记录

- 每一步是否和「预期」一致；如果不一致，记下那一步的输出（核对脚本的表和计数、浏览器里的错误提示、后端窗口的最后几行）。
- 特别是：重启后有没有出现 `session_not_found`（404）或 `recovery_pending`（409），有没有会话的 `inflight` 是 `True`，以及最终 `action_receipts` 是不是 1。
