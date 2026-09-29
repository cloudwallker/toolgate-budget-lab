"""Reproducible two-process demos and load evidence, using only the test database."""

import argparse
import asyncio
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from process_lab import ProcessLab
from run_tests import test_environment
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]


def percentile(values, fraction):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 3) if values else None


def summarize(values):
    return {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def measurement(response, index, elapsed):
    body = response.json()
    timing = dict(re.findall(r"([a-z_]+);dur=([0-9.]+)", response.headers.get("server-timing", "")))
    return {
        "request": index,
        "api_process": index % 2,
        "http_status": response.status_code,
        "classification": body.get("error", body.get("state", "unknown")),
        "elapsed_ms": round(elapsed * 1000, 3),
        "admission_ms": float(timing["admission"]) if "admission" in timing else None,
        "principal_lock_ms": float(timing["principal_lock"])
        if "principal_lock" in timing
        else None,
    }


async def send(lab, index):
    started = perf_counter()
    response = await lab.call(index % 2)
    return measurement(response, index, perf_counter() - started)


async def journal(lab):
    intervals = []
    events = await lab.monotonic_journal()
    starts, terminals = {}, {}
    active = peak = 0
    last_time = -1
    for index, event in enumerate(events):
        assert event["seq"] == index + 1
        assert event["time_ns"] >= last_time
        last_time = event["time_ns"]
        operation_id = event["operation_id"]
        if event["event"] == "started":
            assert operation_id not in starts
            starts[operation_id] = event["time_ns"]
            active += 1
        else:
            assert event["event"] == "terminal" and operation_id in starts
            assert operation_id not in terminals
            terminals[operation_id] = event["time_ns"]
            active -= 1
        assert active >= 0
        peak = max(peak, active)
    assert active == 0 and peak <= lab.slots
    calls = await lab.calls()
    assert set(starts) == set(terminals) == {str(call.id) for call in calls}
    for call in calls:
        operation = await lab.tools.get(call.id)
        if operation is None:
            raise AssertionError("admitted call has no tool start record")
        if operation.completed_at is None:
            raise AssertionError("report requires every tool to have finished")
        operation_id = str(operation.operation_id)
        intervals.append(
            {
                "operation_id": operation_id,
                "state": operation.state,
                "started_at": operation.started_at.isoformat(),
                "completed_at": operation.completed_at.isoformat(),
                "wall_duration_ms": round(
                    (operation.completed_at - operation.started_at).total_seconds() * 1000, 3
                ),
                "start_monotonic_ns": starts[operation_id],
                "terminal_monotonic_ns": terminals[operation_id],
                "duration_ms": round((terminals[operation_id] - starts[operation_id]) / 1e6, 3),
            }
        )
    return intervals, events, peak


async def collect(lab, scenario, rows, started, *, barrier_held=None):
    intervals, events, peak = await journal(lab)
    budget = await lab.budget()
    assert budget["inflight"] == 0
    assert budget["used"] == len(intervals) <= lab.daily
    return {
        "scenario": scenario,
        "requests": len(rows),
        "daily_limit": lab.daily,
        "max_inflight": lab.slots,
        "barrier_held": barrier_held,
        "counts": dict(Counter(row["classification"] for row in rows)),
        "admitted": budget["used"],
        "tool_started": len(intervals),
        "tool_peak": peak,
        "end_inflight": budget["inflight"],
        "wall_seconds": round(perf_counter() - started, 3),
        "latency_ms": summarize([row["elapsed_ms"] for row in rows]),
        "admission_ms": summarize(
            [row["admission_ms"] for row in rows if row["admission_ms"] is not None]
        ),
        "principal_lock_ms": summarize(
            [row["principal_lock_ms"] for row in rows if row["principal_lock_ms"] is not None]
        ),
        "tool_duration_ms": summarize([operation["duration_ms"] for operation in intervals]),
        "raw_requests": rows,
        "tool_journal": intervals,
        "tool_events": events,
        "wall_clock_regressions": sum(row["wall_duration_ms"] < 0 for row in intervals),
    }


async def normal(count):
    async with ProcessLab(daily=count, slots=2, delay_seconds=0.2) as lab:
        lab.release()
        semaphore = asyncio.Semaphore(2)

        async def paced(index):
            async with semaphore:
                return await send(lab, index)

        started = perf_counter()
        rows = await asyncio.gather(*(paced(index) for index in range(count)))
        assert all(row["classification"] == "succeeded" for row in rows)
        await lab.assert_finished(count)
        return await collect(lab, "normal-paced", rows, started)


async def competition(count, *, daily, slots, accepted, name):
    async with ProcessLab(daily=daily, slots=slots, delay_seconds=0.2) as lab:
        barrier = asyncio.Event()

        async def contender(index):
            await barrier.wait()
            return await send(lab, index)

        started = perf_counter()
        tasks = [asyncio.create_task(contender(index)) for index in range(count)]
        barrier.set()
        try:
            await lab.wait_started(accepted)
            for _ in range(300):
                if sum(task.done() for task in tasks) == count - accepted:
                    break
                await asyncio.sleep(0.02)
            assert sum(task.done() for task in tasks) == count - accepted
            held = (await lab.budget())["inflight"]
            assert held == accepted
            lab.release()
            rows = await asyncio.gather(*tasks)
            await lab.assert_finished(accepted)
            assert sum(row["http_status"] == 429 for row in rows) == count - accepted
            return await collect(lab, name, rows, started, barrier_held=held)
        finally:
            lab.release()
            await asyncio.gather(*tasks, return_exceptions=True)


async def lost_response():
    async with ProcessLab(daily=10, slots=1, drop_response=True) as lab:
        lab.release()
        started = perf_counter()
        row = await send(lab, 0)
        assert row["http_status"] == 202 and row["classification"] == "unknown"
        call = (await lab.calls())[0]
        denied = await send(lab, 1)
        assert denied["classification"] == "CONCURRENCY_EXHAUSTED"
        assert (await lab.budget())["inflight"] == 1
        await asyncio.gather(*(lab.reconcile(call.id) for _ in range(5)))
        await lab.assert_finished(1)
        result = await collect(
            lab, "terminal-response-lost", [row, denied], started, barrier_held=1
        )
        result["readonly_reconciliations"] = 5
        result["release_audits"] = 1  # assert_finished checked the real audit table.
        return result


async def crashed_api():
    async with ProcessLab(daily=10, slots=1) as lab:
        started = perf_counter()
        pending = asyncio.create_task(lab.call(0))
        await lab.wait_started(1)
        call = (await lab.calls())[0]
        lab.kill_api(0)
        await asyncio.gather(pending, return_exceptions=True)
        held = (await lab.budget())["inflight"]
        assert held == 1
        denied = await send(lab, 1)
        assert denied["classification"] == "CONCURRENCY_EXHAUSTED"
        lab.release()
        await lab.wait_terminal(call.id)
        await lab.reconcile(call.id)
        await lab.assert_finished(1)
        result = await collect(lab, "api-process-killed", [denied], started, barrier_held=held)
        result["killed_api_requests"] = 1
        result["release_audits"] = 1
        return result


def markdown(report):
    lines = [
        "# 双 API 进程负载与故障报告",
        "",
        f"执行时间（UTC）：{report['executed_at']}。数据来自本次真实运行，没有模拟吞吐数字。",
        "",
        f"环境：{report['environment']['os']} {report['environment']['architecture']}；"
        f"Python {report['environment']['python']}；"
        f"PostgreSQL {report['environment']['postgresql']}。",
        "两个独立 API 进程、一个独立工具进程共享本机 Docker PostgreSQL，HTTP 使用 loopback。",
        "",
        "| 场景 | HTTP 请求 | 准入/工具启动 | 拒绝 | 工具峰值/上限 | "
        "准入 P50/P95 ms | 锁获取 P50/P95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in report["scenarios"]:
        rejected = sum(
            value for key, value in result["counts"].items() if key.endswith("EXHAUSTED")
        )
        admission, locking = result["admission_ms"], result["principal_lock_ms"]
        lines.append(
            f"| {result['scenario']} | {result['requests']} | "
            f"{result['admitted']}/{result['tool_started']} | "
            f"{rejected} | {result['tool_peak']}/{result['max_inflight']} | "
            f"{admission['p50']}/{admission['p95']} | {locking['p50']}/{locking['p95']} |"
        )
    lines += [
        "",
        "normal-paced 使用两个客户端并发、每次工具固定延迟 200ms；"
        "争抢场景用同步客户端起跑屏障和工具挂起屏障，拒绝完成后才放行工具。",
        "",
        "admission 是 Server-Timing 记录的准入到提交确认耗时；principal_lock 包括锁取得的 SQL/"
        "连接往返与等待，不是 PostgreSQL 内部纯锁等待。HTTP 全程延迟含工具或屏障等待，"
        "不能当成预算耗时。工具峰值和耗时来自独立工具进程的单调时钟事件；"
        "持久数据库 started/completed 记录核对操作身份和不可变终态。",
        "",
        "API 强杀请求没有可返回的 HTTP 响应，单独计入 killed_api_requests；表中该场景的"
        "一个 HTTP 请求是由存活 API 返回的拒绝。响应丢失场景工具已持久终态，网关仍 unknown，"
        "并发对账只释放一次。每个场景结束均断言 inflight=0，日额不退款。",
        "",
        "原始脱敏数据：[load-results.json](../../artifacts/load-results.json)。"
        "不包含 Key、认证头、个人目录、完整参数或工具结果。",
        "",
        "这是一台本机的受控实验，不能外推生产吞吐或不同网络环境的 P95。工具生命周期单调"
        "事件在业务前记录 started、在终态证据对外可见前记录 terminal；原始 wall 时间亦保留，"
        "若回拨由 wall_clock_regressions 标记。API/工具进程启动时间不计入 wall_seconds。",
        "",
    ]
    return "\n".join(lines)


async def run(count):
    async with ProcessLab(daily=1, slots=1) as lab:
        async with lab.database.sessions() as session:
            postgres = await session.scalar(text("SHOW server_version"))
    environment = {
        "os": platform.system() + " " + platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "postgresql": postgres,
        "cpu_count": os.cpu_count(),
        "api_processes": 2,
        "tool_processes": 1,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("fastapi", "sqlalchemy", "httpx", "psycopg")
        },
    }
    scenarios = [
        await normal(count),
        await competition(count, daily=3, slots=count, accepted=3, name="daily-three-race"),
        await competition(count, daily=count, slots=2, accepted=2, name="concurrency-two-race"),
        await lost_response(),
        await crashed_api(),
    ]
    return {
        "format_version": 1,
        "executed_at": datetime.now(UTC).isoformat(),
        "environment": environment,
        "scenarios": scenarios,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    args = parser.parse_args()
    if not 20 <= args.requests <= 500:
        parser.error("requests must be between 20 and 500")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    os.environ.update(test_environment())
    report = asyncio.run(run(args.requests))
    raw, document = ROOT / "artifacts/load-results.json", ROOT / "docs/reports/load-report.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    document.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    document.write_text(markdown(report), encoding="utf-8")
    print(
        "Verified five scenarios. Wrote artifacts/load-results.json "
        "and docs/reports/load-report.md."
    )


if __name__ == "__main__":
    main()
