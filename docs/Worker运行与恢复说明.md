# Worker 运行与恢复说明

操作入口见[使用说明](数据与使用说明.md)，实测结果见[变更与验收](变更与验收.md)。

## 1. Worker、进程、线程和 Task

`API/检测器 → 数据库 Task → Worker → Agent → 工具/Checkpoint/报告 → Task 终态`

| 概念 | 本项目含义 |
|---|---|
| Worker | 后台执行任务的角色，不是线程的别名 |
| 进程 | API、Worker、scheduler 通常分别运行，内存不共享 |
| 线程 | Worker 用专用线程池运行同步 Agent；同进程共享内存，数据库 Session 不跨线程共用 |
| asyncio 协程 | 调度领取、续租和等待，通过 await 让出执行权 |
| 数据库 Task | 持久化业务任务，进程退出后仍存在，不等同于 asyncio.Task |

`WORKER_CONCURRENCY=4` 限制每个 Worker 的在途任务数。先检查空闲槽位再领取，避免大量任务标成 running 却排队等待执行。控制操作与诊断线程池分开，减少长模型调用阻塞心跳的风险；数据库本身仍可能阻塞。

## 2. 状态决定能否恢复

| 情况 | Task | Checkpoint | 处理 |
|---|---|---|---|
| 完整步骤已保存、继续调查 | running | active | 保存下一步，崩溃后由队列判断是否可继续 |
| 明确临时失败且预算允许 | retrying | waiting_retry | 等退避/Retry-After 后恢复，不把失败结果当成功缓存 |
| 报告合格，原因可以未确认 | succeeded | completed | 复用已有终态，不再次执行 |
| 永久/未知故障或不再允许重试 | failed | failed | 保留错误，等待人工处理 |
| 步数/token/时间耗尽或报告不合格 | incomplete | stopped | 保留未完成结果，不自动补发模型请求 |

早期 completed 中的失败/不完整结果按旧终态兼容读取，不自动重放。恢复前核对 run_id 对应的 task_id、商品、窗口和异常 ID；旧 active 缺预算、JSON 损坏或身份不匹配时拒绝自动恢复。

Checkpoint 只保存**最新完整步骤**，包含 messages、候选假设、成功证据、工具历史、下一步、累计已知用量与原始预算。它不是任意历史回滚；已保存步骤可复用，工具完成但未落盘时仍可能重做。

## 3. 租约、心跳与旧执行者写保护

每次领取生成独立 `lease_token`，设置 `lease_until`，心跳更新 `heartbeat_at` 并续租。失效租约可能意味着进程退出，也可能是暂时卡住，不能假设旧线程已经停止。

因此写入报告、checkpoint 和任务状态前，必须在事务里锁定并核对 Task 的 running 状态、尝试编号、凭证与租约。旧执行者迟到结果应被拒绝，这就是 fencing。续租失败会标记本地所有权失效，不能继续把迟到结果写成成功。

心跳只证明续租逻辑还在工作，不证明模型取得进展；所以首次领取同时固定 `deadline_at`。领取与崩溃回收共用身份、终态、次数和预算检查，已有终态优先同步，否则仅在剩余预算内回队，不会因重启获得新预算。绝对截止时间依赖系统时钟正常。

实现入口：[worker.py](../app/tasks/worker.py)、[queue.py](../app/tasks/queue.py)、[task_ownership.py](../app/task_ownership.py)、[checkpoint.py](../app/agent/checkpoint.py)。

## 4. 重试次数与预算

下表是代码及模板默认值，实际运行以部署配置为准。任务尝试次数、Agent 步数和 Provider 请求次数是不同概念。

| 配置 | 默认值 | 语义 |
|---|---|---|
| LLM_MAX_RETRIES | 3 | 每次 chat 额外重试，最多 4 次请求；范围 0～5 |
| TASK_MAX_RETRIES | 3 | 包含首次的任务总尝试次数；范围 1～10 |
| LLM_TIMEOUT_SECONDS | 60 秒 | 默认 chat 调度预算及单次网络阶段超时上限，调用方可收紧/指定调度预算 |
| AGENT_STEP_TIMEOUT_SECONDS | 90 秒 | Agent 单轮调用预算，再受剩余总时间约束 |
| AGENT_TOTAL_TIMEOUT_SECONDS | 300 秒 | 从首次执行计时，包含失败后的排队等待，恢复不重置 |
| AGENT_MAX_STEPS / AGENT_TOKEN_BUDGET | 8 / 30000 | 累计步骤与已知输入+输出 token 上限 |
| AGENT_MAX_OUTPUT_TOKENS | 2048 | 请求前按剩余预算进一步收紧单次输出；预算少时停止扩展调查、优先收尾 |
| TASK_RETRY_BACKOFF_SECONDS | 5 秒 | 任务退避基数，结合服务端要求等待 |
| TASK_LEASE_SECONDS / TASK_HEARTBEAT_SECONDS | 60 / 15 秒 | 心跳间隔不得超过租约的 1/3 |
| TASK_POLL_INTERVAL_SECONDS / TASK_RECOVERY_INTERVAL_SECONDS | 2 / 15 秒 | 正常领取轮询与失效租约扫描 |

[Provider](../app/llm/deepseek.py)关闭 SDK 内置重试，由一层代码统一分类、随机退避和剩余时间检查，避免嵌套重试放大。

Agent 已加入请求前输入估计与输出限制，优先为结论和修正留下空间；估计无法替代供应商实际账单，不保证硬封顶。checkpoint 保存启动时的人工经验引用及上下文，恢复不重新检索或重置预算。经验后来停用只影响新诊断，不能撤回已开始执行或已发出的请求。

| 故障 | 策略 |
|---|---|
| 临时网络故障，408/429/500/502/503/504 | 在次数和时间允许时重试 |
| 已识别的认证、权限、参数、额度/余额错误 | 停止；等待不能自动修复 |
| TLS、本地配置/协议错误、未知异常 | 不盲目重试，不关闭证书校验 |
| 服务端 x-should-retry:false | 停止；true 不提升其他不可重试错误 |
| Retry-After / retry-after-ms | 尊重最短等待；等待过长或预算不足时交给上层判断，不能缩短等待后强发 |

同一步持续临时失败，默认理论上可能出现 3 × 4 次请求，仍受总预算限制；这不是整个诊断只有 12 次请求。多个步骤各自可能重试。`llm_calls` 记录成功的逻辑调用，`llm_attempts` 记录已知 Provider 尝试，未知失败用量不构成完整账单。超时不保证服务方停止处理或计费。

## 5. 启动、停止和恢复排查

先盘点，再按批准范围启动一个 Worker：

```powershell
.\.venv\Scripts\python.exe scripts/worker_preflight.py
.\.venv\Scripts\python.exe scripts/run_worker.py
```

盘点脚本只做聚合 SELECT，不建表、不领取/回收、不调用模型，不打印任务正文或凭证。候选数可能重叠，未解码全部 checkpoint，只是快照，不是启动许可。

前台 Ctrl+C 停止领取并等待在途任务；不要用关闭窗口或强杀替代正常排空。`manage_services.py stop` / `stop_all.bat` 使用 taskkill /F，必须按有副作用操作对待。Worker 自己会周期回收，恢复不依赖 scheduler 必须启动。

遇到 retrying/running 先看任务的 attempts、retry_after、deadline_at、heartbeat_at、lease_until，再按 run_id 查存档与日志。历史 failed/incomplete 不自动重跑；重复 API 请求的幂等键可能命中旧记录，人工重新诊断入口尚需专门设计。

## 6. 旧库升级和备份

旧 task 表需要 `lease_token VARCHAR(32)`、`lease_until DATETIME`、`heartbeat_at DATETIME`、`deadline_at DATETIME` 四个可空列。create_all 不会给已有表补列；启动遇到缺列会拒绝继续，避免旧结构被新 Worker 使用。

按以下顺序操作，每项真实变更需明确批准：

1. 确认所有相关旧 Worker/API/scheduler 停止，避免新旧版本混跑和备份期间 DDL。
2. 核对目标库，用备份脚本的 **-NoPrune** 保留所有旧备份；检查导出退出码和文件非空。
3. 先运行迁移预览，确认只有预期增列。
4. 明确停服务且已备份后应用，再预览复核。
5. 单独启动 API、检查 /healthz，盘点队列，再启动 Worker 并提交授权数量的受控新任务。

```powershell
# 仅预览
.\.venv\Scripts\python.exe scripts/migrate_worker_schema.py

# 真实修改；标志是操作者声明，不会替你停止进程
.\.venv\Scripts\python.exe scripts/migrate_worker_schema.py --apply --workers-stopped
```

备份脚本为 [backup_db.ps1](../scripts/backup_db.ps1)，在既有脚本策略允许且经批准时运行 `powershell -NoProfile -File scripts/backup_db.ps1 -NoPrune`；策略拒绝时先处理授权，不默认绕过。默认 Keep=7 会在成功导出后清理符合规则的旧 SQL 文件，迁移前不要省略 -NoPrune。

脚本让 mysqldump 直接写唯一 `.sql.partial`，确认退出码 0 且非空后发布 `.sql`；失败不发布、不清理旧备份。密码不放命令参数，但子进程环境仍应保密。成功导出只证明导出完成，必须在隔离库验证恢复；MySQL DDL 可能隐式提交，增列部分成功后可再预览，不承诺事务回滚。

已升级环境无需重复增列；是否迁移以只读预览为准。备份文件和恢复证据应留在受控环境，不上传公开仓库；验证范围见[变更与验收](变更与验收.md)。

## 7. 尚不能承诺的保证

- 报告事务与最终 checkpoint 分开；保存报告后、存档前崩溃仍有恢复窗口。
- 终态存档与告警不是事务 outbox，可能漏发；接收方幂等键不能保证投递恰好一次。
- 当前工具为读取；以后写库存/改价等必须另做幂等、审批、事务或补偿，checkpoint 不能代替。
- 线程取消不是硬终止，网络或 DB 阻塞会影响排空/续租；模型请求可能仍在远端计费。
- SQLite 离线故障测试不能代替真实 MySQL 多进程竞争、断连和崩溃接管演练。
