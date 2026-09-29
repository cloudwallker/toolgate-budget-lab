# 真实进程验收

实验使用 Python 3.12、PostgreSQL 测试数据库、两个独立 Uvicorn API 子进程和一个独立工具子进程。HTTP 请求实际经过 `127.0.0.1` TCP，不使用 ASGI transport 代替跨进程网络。

运行：

```powershell
./scripts/test.ps1 -TestArguments @('tests/process', '-q')
```

`scripts/test.ps1` 从本地配置设置 `TOOLGATE_TEST_DATABASE_URL` 与 `MOCK_TOOLS_TEST_DATABASE_URL`。缺少连接配置时测试报错，不跳过关键验收。网关测试连接要求数据库名称以 `_test` 结尾。工具连接使用独立工具角色。

## 验证内容

| 验收 | 真实进程测试与证据 |
| --- | --- |
| A03 | 两 API 同步提交 20 个不同键请求，日额 1、并发上限 20：只准入 1 个 |
| A04 | 日额 30、并发上限 2：工具保持在文件屏障处，恰好 2 个占位，18 个 429 |
| A05 | 日额 3、并发上限 20：恰好 3 个准入，17 个 429；重启 API 后使用量保持 |
| A06 | 同键同参 20 个跨进程请求：只启动一个工具、扣额一次；异参返回 409 |
| A08 客户端断连 | 原始 TCP 客户端发送完整请求、等工具 started 后关闭连接；远程工具仍未结束，另一 API 拒绝新请求 |
| A09 | 工具 started 后强制结束自有 API PID；另一 API 保持占位，running 对账不释放；工具终态后只读对账释放 |
| A10 | 工具持久写入终态后，实验 ASGI 包装器发送长度不足的真实 HTTP 响应；网关返回 202、保留占位；五个并发只读对账后审计 RELEASED 恰好一条 |

所有完成场景检查日额、网关调用数、工具启动标记、独立工具数据库的持久终态以及 RELEASED 审计条数。原始数据库墙钟开始/完成时间完整保留；实验服务额外持久记录单调时钟生命周期，用后者重建执行峰值。工具测试数据库也必须以 `_test` 结尾，检查发生在任何初始化之前。网关清理仅针对本次随机 principal；不可变工具记录保留，不尝试删除。

单调日志为临时目录中的 `lifecycle.jsonl`，每行只含 operation UUID、`started` / `terminal`、`monotonic_ns` 和连续序号。工具 started 记录确认后、业务执行前记开始。对 POST 与 GET 的有效响应，均在向外发送 `http.response.start` 之前查询持久工具终态并写入结束事件；响应丢失实验也经过此观察边界。每次同步写入和 fsync 中间没有 await，因此同一工具进程的事件顺序不会交错。重复查询只记一次 terminal。

验证要求日志 UUID 集合等于本次工具持久记录集合、每个调用恰好两个生命周期事件、序号连续、单调时间不倒退、所有操作结束以及峰值不超过上限。这样既不会把回拨墙钟截为零，也不会因正常请求响应或只读对账先释放、结束采样后发生而高估峰值。

API handler 被取消不同于客户端 TCP 断连，前者由 API 层测试单独覆盖。其余身份隔离、日期切换、提交确认丢失等验收由单元/API/数据库集成层承担；本表不把它们冒充真实进程测试。

## 实验入口与隔离

`mock_tools.lab_server` 必须显式设置 `TOOLGATE_LAB_MODE=1`，控制目录必须是系统临时目录下 `toolgate-lab-` 前缀的专用目录。它只从本地文件读取屏障与响应丢失设置，不提供 HTTP 故障控制或删除接口。模块入口仅绑定 `127.0.0.1`，不进入生产 Compose。

进程由 `subprocess.Popen` 启动；Windows 使用 `CREATE_NO_WINDOW`。结束操作只作用于本实验创建并保存的进程对象。窗口和 PID 消失不作为释放名额的证据。

Windows 下直接通过 `asyncio.run(uvicorn.Server(config).serve())` 启动并设置 Selector policy，避免新版 `uvicorn.run` 的 loop factory 重新选择 Proactor 后导致 psycopg 异步连接报错。

`scripts/process_lab.py` 的 `ProcessLab` 是负载脚本可复用的异步 context manager。`race()` 输出脱敏计数与请求耗时，不输出数据库连接串、Bearer Key、完整参数或结果。
