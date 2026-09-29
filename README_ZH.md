# ToolGate Budget Lab

### 跨 Agent 后端进程的工具调用预算实验

**用两个 API 进程、真实 PostgreSQL 和持久只读工具服务复现额度争抢与故障恢复，验证未知调用保留占位、可信终态只释放一次。**

[English](README.md) | 中文

[快速启动](#快速启动) · [运行实验](#运行实验) · [API 协议](docs/api-and-states.md) · [验收测试](docs/acceptance.md)

![两个 API 进程共享 PostgreSQL 原子准入，并查询工具持久终态证据](assets/architecture.png)

*已实现 HTTP 实验的架构示意。网关与工具 schema 分别由独立数据库角色管理。*

## 功能

- **按身份计额：** Bearer Key 在服务端映射为 principal，工具采用固定允许列表。每个身份独立计算 UTC 日额度和跨日并发占位。
- **原子准入：** 锁定 principal 行，在同一事务更新额度、插入调用与审计；网络通信发生在事务之外。
- **幂等请求：** principal 与 UUID 请求键共同标识调用。同键同参重放不扣额、不重发，异参返回 409。
- **保守恢复：** 超时、取消、客户端断开或 API 崩溃均保留未知占位。恢复只查询原 operation UUID，取得匹配的可信终态后释放。
- **可复现实验：** 真实数据库和独立子进程测试、同步竞争、响应截断、进程强杀、脱敏原始数据与单调生命周期观测。

固定只读工具为 `catalog.lookup`、`catalog.summary`，运行实验无需大模型。

## 快速启动

需要 Docker Engine 和 Compose。首次启动生成随机本地演示凭据；再次启动保留凭据、数据卷和已有额度。

Windows PowerShell：

```powershell
.\scripts\start.ps1
```

Linux/macOS（Python 3.12+）：

```bash
python scripts/bootstrap.py
docker compose up -d --build --wait
```

API A：`http://127.0.0.1:58081`；API B：`http://127.0.0.1:58082`；工具服务：`http://127.0.0.1:58083`。

就绪检查：`GET /health/ready`。演示账号 `alpha` 可用两个工具，`beta` 只可用 lookup；各有日额度 3、并发上限 2。Key 位于忽略的本地 `.env`，示例配置只有占位符。

```http
POST /v1/calls
Authorization: Bearer <ALPHA_KEY>
Idempotency-Key: <new UUID>
Content-Type: application/json

{"tool":"catalog.lookup","arguments":{"sku":"SKU-001"}}
```

终态返回 200；仍持位的未知调用返回 202，并附调用 ID 和查询入口。用同一身份查询 `/v1/calls/{id}` 或 `/v1/budget`。日额度计量成功准入的唯一调用，失败或安全终止均不退款。

停止：`docker compose down` 或 `.\scripts\stop.ps1`，两者均保留数据。不要提交 `.env`。

## 运行实验

需要 uv 和 Python 3.12，项目本地测试固定为 3.12.12。

```bash
uv sync --frozen
uv run python scripts/bootstrap.py
docker compose up -d --wait postgres
uv run python scripts/run_tests.py
uv run ruff check .
uv run ruff format --check .
uv run python scripts/load_lab.py --requests 100
```

测试使用独立 `toolgate_test` 数据库、随机 principal 和自有子进程；关键数据库测试缺少配置时明确失败。工具不可变证据保留在测试库，网关仅清理每轮实验自己创建的身份记录。

负载脚本在临时本机端口启动两个 API 进程和一个工具进程，演示正常调用、日额度竞争、并发竞争、终态响应丢失和 API 强杀，不消耗开发账号额度。

发布样本：64 项本地测试通过；全新卷启动和跨 API 重放通过。100 请求争抢中，日额 3 只准入 3 次，并发上限 2 在工具屏障挂起期间只准入 2 次。[负载报告](docs/reports/load-report.md)与[脱敏原始数据](artifacts/load-results.json)记录环境和计时定义。单机正确性实验不代表生产吞吐。

受控恢复命令：

```bash
docker compose run --rm api-a reconcile --call-id <CALL_UUID>
docker compose run --rm api-a reconcile --reserved --limit 100
```

第一条只查询原操作；第二条仅安全终止过期且仍 reserved 的调用，不能无证据回收 dispatching 或 unknown。

## 文档

- [API 与状态](docs/api-and-states.md)
- [A01～A14 验收覆盖](docs/acceptance.md)
- [真实进程测试与观测边界](docs/process-tests.md)
- [四组演示与面试讲解](docs/demo-and-interview.md)
- [负载报告](docs/reports/load-report.md)

详细指南目前为中文。GitHub Actions 配置执行数据库/进程测试、Lint 和 Compose 启动；远端当前结果以仓库 Actions 为准。

## 保证边界

实验假设所有工具执行经过网关、每次准入最多启动一个操作、上游终态日志可信。发送 HTTP 前必须确认数据库事务提交，关闭自动重试和重定向。已经取得发送权的调用，只凭匹配终态释放；未取得发送权的 reserved 可在同一锁和状态检查下安全终止。

工具无法提供终态时，unknown 可能长期占位。HTTP 首版不声明 MCP 兼容、分布式 exactly-once、任意工具安全或生产部署就绪。网关和工具写入独立 schema，通过固定 HTTP 协议通信。

## 许可证

[MIT](LICENSE)，署名 cloudwallker。
