# 首版验收对照与证据边界

A01～A14 的测试入口和证据边界如下。发布样本包含64项通过的本地测试；远端结果以 GitHub Actions 为准。

## A01～A14 对照

所有数据库行为使用真实 PostgreSQL。下表明确区分真实 TCP/进程实验、ASGI 与 HTTP 故障替身、时钟替身。

| 编号 | 实现及测试入口 | 证据类型与关键断言 |
| --- | --- | --- |
| A01 身份覆盖与查询隔离 | [身份配置](../src/toolgate/config.py)；[严格契约测试](../tests/unit/test_contracts.py)；[API 测试](../tests/api/test_gateway.py)：`test_auth_validation_and_tool_denial_never_send_or_leak`、`test_success_replay_and_query_isolation` | ASGI＋真实数据库：actor 字段拒绝，Key 只映射配置身份，其他身份看不到调用；错误不包含原输入或 Key。 |
| A02 工具权限拒绝 | [预算测试](../tests/integration/test_budget.py)：`test_authorization_rejection_has_no_call_or_credit`；API 的拒绝测试 | 真实数据库＋HTTP 发送哨兵：禁止工具返回 403，无准入与扣额，不进入工具发送入口。 |
| A03 最后一份日额度 | 预算测试：`test_last_daily_credit_cannot_be_admitted_twice`；[进程测试](../tests/process/test_process_lab.py)：`test_two_api_processes_share_admission[1-20-1]` | 数据库竞争及两个独立 API 进程：20 个请求只有一次新准入；重启 API 不重置使用量。 |
| A04 最后两个并发名额 | 预算测试：`test_concurrency_slot_and_idempotency_are_shared_between_store_instances`；进程测试 `[30-2-2]` | 起跑屏障＋独立工具挂起：恰好两条准入并持位，其余拒绝；工具进程观测峰值不超过上限。 |
| A05 剩余三份日额度 | 进程测试 `[3-20-3]`；[负载入口](../scripts/load_lab.py)：`daily-three-race` | 两 API 与独立工具服务：仅三次准入，数据库使用量与工具启动数对应；负载场景默认 100 请求。 |
| A06 请求键幂等 | 预算及 API 重放测试；进程测试：`test_same_key_across_processes_starts_one_tool` | 同键竞争只扣一次、取得一次发送权、启动一个工具；异参 409。同键作用域包含 principal。 |
| A07 reserved 回收竞争 | 预算测试：`test_reserved_abort_competes_with_dispatch_and_daily_credit_is_not_refunded`；[回收发送测试](../tests/integration/test_aborted_dispatch.py)：`test_reclaimed_reservation_cannot_start_tool` | 真实数据库 CAS：回收与发送权仅一方获胜；已 aborted 的调用不能发送；日额度不退款。 |
| A08 停止等待与取消 | API 测试：`test_total_deadline_and_cancel_keep_unknown_held`；进程测试：`test_client_tcp_disconnect_does_not_end_live_tool` | 前者用 HTTPMockTransport 控制总超时与服务端任务取消；后者关闭真实客户端 TCP。两种事件分别测试，不把 TCP 断开等同于 handler 被取消。工具未确认结束时占位保留。 |
| A09 API 强杀 | 进程测试：`test_killed_api_cannot_release_live_tool` | 工具 started 后强杀本测试创建的 API 子进程；另一 API 拒绝超额调用。工具终态之前不释放，之后只读对账释放。 |
| A10 终态响应丢失 | 进程测试：`test_terminal_response_loss_reconciles_once_without_resend`；预算终态测试 | 独立工具进程持久化终态后，故障入口截断 HTTP 响应。网关 unknown；并发 GET 对账后 RELEASED 审计恰好一条，无重发或再次扣额。 |
| A11 无可信终态 | API 测试：`test_untrusted_response_preserves_slot`、`test_reconcile_only_terminal_and_reserved_reap`；[真实对账测试](../tests/process/test_reconcile_unavailable.py) | HTTP 故障替身＋真实数据库：错误 operation、非法 schema、running、500、重定向均不释放。真实 TCP 查询原操作 404、工具进程不可达均保留占位，不把查无记录解释为未发送。 |
| A12 持久化故障 | [事务故障测试](../tests/integration/test_transaction_faults.py)：`test_audit_failure_rolls_back_admission_credit_and_call`、`test_admission_commit_confirmation_loss_leaves_replayable_reserved_call`、`test_dispatch_commit_confirmation_loss_cannot_reclaim_or_dispatch_again`；API 的提交确认丢失及审计故障测试 | 真数据库触发器拒绝审计，断言准入/扣额整体回滚；确认丢失则在真实提交后用边界替身抛错，可能留下完整 reserved/dispatching 记录。HTTP 发送哨兵断言本执行者不发送；不声称注入了真实数据库网络断连。 |
| A13 UTC 日切换 | 预算测试：`test_day_rollover_does_not_reset_old_unknown_occupancy` | 注入固定时间的时钟替身＋真实数据库：新日独立计额，旧 unknown 跨日占位。不是真实等待午夜实验，也不证明系统墙钟永不回拨。 |
| A14 principal 独立 | 事务故障测试：`test_distinct_principals_share_neither_idempotency_nor_quota`；API 隔离测试 | 同一请求键在两个 principal 下各有独立调用与额度；对方调用查询不可见。 |

## 复跑与报告

依赖、启动及本地凭据生成见 [README](../README.md)。测试入口会从本地配置派生独立 `toolgate_test` 连接，不输出凭据；数据库和进程测试缺配置直接失败，不跳过。

```bash
uv run python scripts/run_tests.py
uv run ruff check .
uv run ruff format --check .
uv run python scripts/load_lab.py --requests 100
```

负载结果已写入 [脱敏原始 JSON](../artifacts/load-results.json) 与 [报告](reports/load-report.md)。工具峰值采用工具进程的单调生命周期事件，避免墙钟回拨造成错误区间；原始数据同时保留 wall 时间供核查。

`admission` 测量到提交确认；`principal_lock` 包括 SQL 往返与锁获取等待，不是 PostgreSQL 内部纯锁等待。HTTP 全程延迟包含工具及屏障等待，不能当成预算决策耗时。单机受控实验不证明生产吞吐、公网安全或跨数据库集群正确性。
