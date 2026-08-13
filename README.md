# 电商商品经营异常诊断 Agent

> SQL/Python 负责**发现问题**，Agent 负责**调查问题**，LLM 负责**决策与总结**，Tool 负责**取证**。
> 数据库负责记忆，Context 负责当前思考。

## 架构

```
events.csv ──导入聚合──► daily_item_stat 宽表(UV/曝光/点击/加购/成交/GMV 按天×商品×维度)
                              │ 规则引擎(不碰LLM, 确定性、可单测)
                              ▼
                        anomaly_event 异常事件
                              │ 生成 Task
                              ▼
                     DB 任务队列 + asyncio Worker(并发限制/重试/幂等/优先级/状态)
                              ▼
         Workflow 固定流程: 确认异常→历史趋势→定位环节→维度拆解→综合证据→报告
                              ▼
                 Agent Loop (max_steps / timeout / token预算 / 工具白名单)
                 ├─ MetricTool ─┐ 全部走 agent_ro 只读连接(仅SELECT)
                 ├─ FunnelTool ─┤ 参数化SQL + 结果上限 + 审计日志(run_id)
                 └─ DimensionTool┘
                              ▼
             结构化报告(现象-原因-建议) + 告警(接口预留)
                ▲
      FastAPI 层: 触发诊断/查任务/查报告/CSV导入/反馈 ──┐
      日志: logs/agent_runs·tool_calls·sql_logs (run_id贯穿) │
      评估: evaluation/   反馈: feedback/agent_feedback/    ──┘
```

## 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.14 + venv | 环境隔离 |
| Web | FastAPI + uvicorn | 原生 async、自动 OpenAPI 文档 |
| DB | MySQL 8.0（本机 3309 端口） | 双账号：`agent_app`(写/DDL) + `agent_ro`(仅 SELECT) |
| LLM | DeepSeek API（OpenAI 兼容协议） | base_url/model 全配置化；未填 Key 自动用 MockLLM |
| Agent | 纯 Python 手写 Agent Loop | 不依赖 LangChain，可读可审计 |

## 快速开始

```bash
# 1. 环境
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置（复制 .env.example 为 .env，按需修改）
#    DB_* / LLM_API_KEY / LLM_MODEL

# 3. 初始化数据库与账号（root 执行一次）
mysql -h 127.0.0.1 -P 3309 -u root -p < scripts\create_users.sql
# 开启 LOAD DATA LOCAL INFILE（导入提速用，root 执行一次；重启后需重开）
mysql -h 127.0.0.1 -P 3309 -u root -p -e "SET GLOBAL local_infile = 1"

# 4. 导入 Retailrocket 数据（约 2.76M 事件）
python scripts\import_retailrocket.py

# 5. 运行异常检测（生成 anomaly_event）
python scripts\run_detection.py

# 6. 启动 API
uvicorn app.api.main:app --reload --port 8000

# 7. 启动 Worker（消费任务队列，另开终端）
python scripts\run_worker.py
```

## API 一览（前缀 `/api/v1`，需 `X-API-Key` 头）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（无需 Key） |
| POST | `/diagnostics` | 提交诊断（`sync=true` 同步返回；否则进任务队列） |
| GET | `/tasks/{task_id}` | 查询任务状态与结果 |
| POST | `/import/daily-stat` | CSV 导入日表（写入口，仅服务层） |
| POST | `/feedback` | 用户对报告打分/反馈 |
| GET | `/docs` | Swagger 文档 |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/diagnostics \
  -H "X-API-Key: dev-key-123" -H "Content-Type: application/json" \
  -d '{"item_id": 355908, "start_date": "2015-06-01", "end_date": "2015-06-14", "sync": true}'
```

## 权限与安全设计

1. **数据库层**：`agent_ro` 仅 `SELECT`（已实测写操作被拒）；Tool 全部参数化 SQL + 窗口/行数上限。
2. **Agent 层**：工具白名单（仅 metric/funnel/dimension）、`max_steps`、单步/总 timeout、token 预算、Context 裁剪。
3. **API 层**：`X-API-Key` 认证 + 每分钟限流 + 文件类型/大小校验 + **安全响应头**（nosniff/X-Frame-Options/no-store/CSP）。
4. **写路径隔离**：Agent 无任何写能力；数据导入/任务状态更新只走服务层。
5. **审计**：每次工具调用写 `tool_call_log`（DB）+ `logs/tool_calls/`（JSONL），全链路 `run_id`。
6. **密钥管理**：真实密码只存 `.env`（已被 git 忽略）；`scripts/create_users.sql` 仅含占位符；MySQL 账号密码已轮换为随机强密码。
7. **密钥防泄漏**：`git commit` 前由 **gitleaks** 自动扫描暂存区（见 `hooks/pre-commit`，运行 `scripts/install_hooks.ps1` 安装）；历史中的旧密码已用 git-filter-repo 清除。

## 可观测性（上线排查）

| 问题 | 排查路径 |
|---|---|
| Agent 突然变慢 | 拿 run_id → `logs/agent_runs/{run_id}.jsonl` 看每步耗时：LLM 慢（查重试/429/模型负载）或 Tool 慢（查 `logs/sql_logs/` 慢查询加索引） |
| 用户反馈不准 | 报告带 run_id → 按三分类定位：数据错（Tool 返回 vs 原始 SQL）→ 口径 bug；工具没调（缺关键工具）→ prompt；分析错（证据与结论矛盾）→ LLM/Context 裁剪 |
| 回归防线 | 修复后跑 `python evaluation/cases_runner.py`，对比 `evaluation_results.json` |

## 目录

```
app/
├── config.py            # pydantic-settings 配置
├── db.py                # 双连接(写/只读) + SQL 日志监听器
├── models.py            # ORM（daily_item_stat/anomaly_event/task/...）
├── tracing.py           # run_id 贯穿 + 三类 JSONL 日志
├── security.py          # API Key + 限流
├── metrics/             # 指标注册表(口径唯一来源) + 计算
├── detection/           # 规则引擎 + 检测器（不碰 LLM）
├── llm/                 # Provider: DeepSeek(OpenAI兼容) / Mock
├── agent/               # Agent Loop / 3 个只读 Tool / Workflow / prompts
├── tasks/               # DB 队列 + asyncio Worker
└── api/                 # FastAPI 路由
scripts/                 # 导入/检测/Worker 脚本
evaluation/              # 黄金用例 + 离线评估
feedback/agent_feedback/ # 用户反馈
logs/                    # agent_runs/tool_calls/sql_logs
tests/                   # pytest（SQLite 全离线）
```

## 里程碑

- [x] V1：数据层 + 规则检测 + Agent Loop + 4 Tool（MockLLM 离线跑通）
- [x] V2：Workflow + 权限（白名单/max_steps/timeout/只读账号/审计）
- [x] V3：REST API + 文件导入 + DB 任务队列 + Worker
- [x] V4：并发限制/重试/幂等/优先级（`app/tasks/`）
- [x] V5：评估（evaluation/）+ 反馈（feedback/）
- [x] P0-P3 增强：PeerTool 跨商品对比、类目级异常聚合、真实 LLM 评估基线、告警 Webhook、监控指标、导入提速 7 倍
- [ ] 后续：价格按日生效 join、Redis/Celery 队列、多 Worker 副本、指标监控面板

## 已知口径（Retailrocket 数据）

- 漏斗为 3 段：`view → addtocart → transaction`（原数据无独立曝光/点击/支付字段）
- 点击率 = 独立浏览用户/曝光次数（代理）
- **GMV = 成交笔数 × 商品最新价格（prop=790 提取，V1 近似）；客单价 = GMV/成交笔数**（真实口径）
- `categoryid`/`available` 未哈希 → 支持类目维度与"下架提示"；其余属性已哈希，系统不解释其含义
- 接入其他真实电商数据时，改 `app/metrics/definitions.yaml` 口径即可，代码不动

## 当前数据状态（2026-08 导入）

| 表 | 行数 | 说明 |
|---|---|---|
| raw_events | 2,756,101 | 原始行为事件（view/addtocart/transaction） |
| daily_item_stat | 6,812,556 | 日聚合宽表（all/day_type/new_user/category 四维度） |
| item_price | 417,053 | 商品最新价格（prop=790） |
| item_category | 417,053 | 商品类目 |
| item_availability | 1,503,639 | 可用性变更日志 |
| anomaly_event | 335 | 规则引擎产出的异常事件（近7日环比/连续下降） |
