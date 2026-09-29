# 双 API 进程负载与故障报告

执行时间（UTC）：2026-09-29T09:08:43.670642+00:00。数据来自本次真实运行，没有模拟吞吐数字。

环境：Windows 11 AMD64；Python 3.12.12；PostgreSQL 17.11 (Debian 17.11-1.pgdg13+2)。
两个独立 API 进程、一个独立工具进程共享本机 Docker PostgreSQL，HTTP 使用 loopback。

| 场景 | HTTP 请求 | 准入/工具启动 | 拒绝 | 工具峰值/上限 | 准入 P50/P95 ms | 锁获取 P50/P95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| normal-paced | 100 | 100/100 | 0 | 2/2 | 30.605/79.993 | 9.595/25.894 |
| daily-three-race | 100 | 3/3 | 97 | 3/100 | 741.884/956.666 | 725.414/945.586 |
| concurrency-two-race | 100 | 2/2 | 98 | 2/2 | 979.406/1545.613 | 964.423/1444.204 |
| terminal-response-lost | 2 | 1/1 | 1 | 1/1 | 39.947/66.625 | 11.448/12.139 |
| api-process-killed | 1 | 1/1 | 1 | 1/1 | 51.493/51.493 | 10.189/10.189 |

normal-paced 使用两个客户端并发、每次工具固定延迟 200ms；争抢场景用同步客户端起跑屏障和工具挂起屏障，拒绝完成后才放行工具。

admission 是 Server-Timing 记录的准入到提交确认耗时；principal_lock 包括锁取得的 SQL/连接往返与等待，不是 PostgreSQL 内部纯锁等待。HTTP 全程延迟含工具或屏障等待，不能当成预算耗时。工具峰值和耗时来自独立工具进程的单调时钟事件；持久数据库 started/completed 记录核对操作身份和不可变终态。

API 强杀请求没有可返回的 HTTP 响应，单独计入 killed_api_requests；表中该场景的一个 HTTP 请求是由存活 API 返回的拒绝。响应丢失场景工具已持久终态，网关仍 unknown，并发对账只释放一次。每个场景结束均断言 inflight=0，日额不退款。

原始脱敏数据：[load-results.json](../../artifacts/load-results.json)。不包含 Key、认证头、个人目录、完整参数或工具结果。

这是一台本机的受控实验，不能外推生产吞吐或不同网络环境的 P95。工具生命周期单调事件在业务前记录 started、在终态证据对外可见前记录 terminal；原始 wall 时间亦保留，若回拨由 wall_clock_regressions 标记。API/工具进程启动时间不计入 wall_seconds。

