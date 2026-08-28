# MCP 接入与学习说明

实验性、可选适配层；代码与隔离测试完成，不继续推进真实接入。验收结果见[变更与验收](变更与验收.md)。

## 1. 目标与范围

将 metric、funnel、dimension、peer 暴露为标准 MCP 工具，复用已有 ToolRegistry 和 SQL。原 Agent 继续直接调用注册表；其他 MCP 客户端通过协议使用相同业务逻辑。

本轮目标是学习协议适配，不是将业务工具做成通用平台。MCP 统一调用方式，不会自动消除电商指标与数据表的业务依赖；无需为实验把原 Agent 改为 MCP 客户端，也不以外部宿主接通作为当前完成条件。

```text
原 Agent ────────────────┐
                        ↓
MCP 客户端 → stdio → 适配层 → ToolRegistry → 只读查询
                        ↓
                结构化证据 + 限制 + 受限日志
```

首版只支持本机 stdio，不开放 HTTP，不执行 Agent/LLM，不创建 Task/报告或数据库表。调用会读数据库并写本地日志，不是无副作用的纯函数。尚无多租户隔离或 MCP 远程 OAuth，不能直接把此入口挂到公网。

## 2. 按步骤理解实现

### 第一步：固定接口，而不是复制业务

[共用 Schema](../app/agent/tools/_common.py)定义正整数商品 ID、严格日期、指标/维度枚举、数组上限和禁止额外参数。四个工具使用同一构造函数；[注册表](../app/agent/tool.py)做 Schema 校验，再由工具检查跨字段日期关系。

为什么：公布 inputSchema 不代表所有 SDK 自动校验；这里由注册表执行，Agent 与 MCP 不采用两套规则。peer 即使没有类目，也先拒绝非法窗口。

### 第二步：让 SDK 负责协议

[适配层](../app/mcp_server.py)使用官方 SDK 低层 Server 的 on_list_tools/on_call_tool。前者把 description/parameters 转为工具定义，后者执行注册表并构造 CallToolResult。不手写 JSON-RPC，也不重新定义四套装饰器函数参数。

requirements.txt 使用的 MCP SDK 为 2.1.1，requirements.txt 固定版本；jsonschema 是直接使用的校验依赖。SDK 2.x 的 Python 字段使用 snake_case，线上字段仍是 inputSchema/structuredContent/isError，由 SDK 转换。

### 第三步：把权限和失败边界带过去

默认关闭；启动者必须提供显式分配 tools:read 的 MCP_ACCESS_KEY。旧 API_KEYS 和 report:read 不自动升级。Key 不是工具参数，模型不能通过 args 自报权限。stdio 信任本机启动者与它连接的客户端，不是逐用户远程身份认证。

只显式导出四个读取工具；以后为 Agent 注册写工具，不会自动成为 MCP 工具。readOnlyHint 只是提示，真正限制来自白名单、校验和数据库账号授权。

服务端生成 run_id 和全局唯一的 call_id（工具名#run_id），客户端不能指定日志路径；原 Agent 的工具名#步骤编号保持不变。成功返回原 data 和服务端 evidence_limits；失败 isError=true、data=null，只返回安全错误码，不向客户端发送 SQL、路径或原始异常。文本与 structuredContent 表达同一个信封，旧客户端也能看到限制；输出也由服务端校验，畸形结果不能成功。

MCP 日志位于 logs/agent_runs、tool_calls、sql_logs，按 run_id 关联；没有写入 agent_run/tool_call_log 等业务表，因此现有 DB 监控面板不统计这些 MCP 调用。原工具/SQL 日志可能含查询信息或异常明文，只能在受限本地查看，不能因客户端错误已脱敏就公开日志。

### 第四步：限制资源，避免假超时

默认每进程 2 个执行槽、每分钟 60 次 tools/call、等待 15 秒、返回体上限 128 KiB。槽满返回 busy，不建立无界查询等待队列。超时或取消不表示 SQL 已停止，直到实际执行结束才释放槽位；没有自动重试。

返回过大时拒绝交付，不静默截断证据。该限制在结果生成后生效，不是数据库扫描、内存或输入协议帧的硬上限；各进程独立计数，不是跨实例限流。

### 第五步：真实协议、隔离业务环境

SDK Client(server) 在进程内做发现与调用，不开端口、不启动外部客户端；复用 tests/conftest.py 的临时 SQLite。auto 模式走 SDK 的现代请求分发，legacy 模式走内存消息流及旧协议握手。二者均已测试，但不是操作系统 stdio 管道验收，也没有连接真实 MySQL。

可重复运行演示（先确认 tests/.pytest_runtime 的绝对路径仍在本项目 tests 内）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_mcp.py -k protocol_walkthrough -s -p no:cacheprovider -q -W error::DeprecationWarning
```

实际输出节选；legacy 模式得到相同业务结果：

```text
[auto] 1. MCP 发现工具：metric, funnel, dimension, peer
[auto] 2. metric：ok=True，rows=14，与原工具 data 一致
[auto] 2. funnel：ok=True，rows=3，与原工具 data 一致
[auto] 2. dimension：ok=True，rows=2，与原工具 data 一致
[auto] 2. peer：ok=True，rows=2，与原工具 data 一致
[auto] 3. 客户端伪造 run_id：拒绝；成功证据包含服务端 evidence_limits
```

[演示测试](../tests/test_mcp.py)中的实际调用方式是：

```python
# config 是测试 fixture 创建的隔离配置，不能拿真实 .env 直接运行此片段。
async with Client(create_server(config=config), cache=None) as client:
    tools = await client.list_tools()
    result = await client.call_tool("metric", {
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14",
    })
    # 先检查 result.is_error，再读取 result.structured_content；保留 evidence_limits。
```

测试还覆盖无授权、非法类型/日期/枚举、未知工具、错误脱敏、输出大小、审计失败、限流、超时与取消后占位、并发日志隔离。没有调用 LLM：MCP 工具服务本身不负责推理，宿主模型能否正确选工具需要另做评估。

## 3. 可选扩展：将来有需求再实际接入

以下仅保留为参考，不是当前待办；本轮不修改真实凭证、不启动 MCP 进程、不接入外部客户端。将来明确需要时，再确认具体范围。

先确认目标库、只读授权、客户端可信范围和日志位置，再在不覆盖原 API_KEY_SCOPES 的前提下添加独立 tools:read 凭证并显式启用。真实 .env 和客户端配置不在本轮自动修改范围。

经批准后，客户端用项目现有解释器执行 scripts/run_mcp.py，cwd 为项目根目录。stdout 只用于协议，调试输出写 stderr。入口不调用 init_db，也不要求启动 API/Worker/scheduler。

按顺序完成，不能把下面步骤理解为已经执行：

1. 确定一个具体客户端；确认它的数据发送策略，工具返回的经营数据可能进入它配置的模型。
2. 确认读取哪个数据库、账号实际只有 SELECT，以及该客户端可以读取全部商品；当前没有店铺过滤。
3. 经批准生成独立随机凭证，在原 API_KEY_SCOPES 中新增 tools:read 条目，不覆盖旧条目。通过受信任启动环境传入 MCP_ACCESS_KEY 和 MCP_ENABLED=true，不把 Key 放在模型参数或公开配置文件。
4. 在该客户端填写 command/args/cwd（具体字段界面由客户端决定）：

| 项目 | 示例值（替换为实际绝对路径） |
|---|---|
| command | C:\path\to\project\.venv\Scripts\python.exe |
| args | scripts/run_mcp.py |
| cwd | C:\path\to\project |

5. 经批准让客户端启动子进程，先只做工具发现，再读取一条批准的商品/日期窗口；核对 isError、data、evidence_limits 和日志，确认没有诊断/写库行为。

本地启动授权不等于操作系统隔离。当前配置模块仍会加载应用配置，进程可能拿到其他应用凭证；远程或多人部署前应拆分最小配置与凭证来源，并增加数据归属控制。环境配置在进程启动时读取，编辑 .env 不会自动撤销运行中进程的权限。

仅查看入口帮助可运行 `.\.venv\Scripts\python.exe scripts/run_mcp.py --help`，此路径在导入应用配置前结束，不启动服务。

## 4. 官方参考

- [Python SDK 2.x](https://github.com/modelcontextprotocol/python-sdk)
- [低层 Server 示例](https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/snippets/servers/lowlevel/direct_call_tool_result.py)
- [客户端与进程内测试](https://py.sdk.modelcontextprotocol.io/client/)

原 Agent 没有改成 MCP 客户端；本机调用继续直连注册表。迁移收益是其他宿主现在有了标准接口，不是强迫每个内部函数调用都经过协议。
