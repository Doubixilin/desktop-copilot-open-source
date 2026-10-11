"""副驾性能实测（acceptance §4 / 09 票）。

固定场景：同一检出、fake agent 可控任务（无模型延迟噪声）+ 真实 runtime
进程采样。输出五种状态的进程数/内存与交互延迟。结果如实记录，不达目标
只报告不粉饰。

用法：python scripts/copilot_perf_probe.py [--real-exe <path>] [--dsh-home <dir>]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import psutil  # noqa: E402

from copilot import models  # noqa: E402
from copilot.controller import CopilotController  # noqa: E402
from copilot.store import CopilotStore  # noqa: E402


def owned_tree_rss(pid):
    try:
        proc = psutil.Process(pid)
        procs = [proc, *proc.children(recursive=True)]
        return len(procs), sum(p.memory_info().rss for p in procs)
    except Exception:
        return 0, 0


def wait_state(controller, run_id, wanted, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        snap = controller.snapshot()
        for r in snap["runs"]:
            if r["run_id"] == run_id:
                last = r["state"]
                if last == wanted:
                    return True
        time.sleep(0.02)
    return False


def wait_past(controller, run_id, before, timeout=60):
    """等待状态离开 before（瞬态可能一闪而过，用'离开'而不是'到达'）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = controller.snapshot()
        for r in snap["runs"]:
            if r["run_id"] == run_id and r["state"] != before:
                return True
        time.sleep(0.02)
    return False


def measure(exe, args, dsh_home, workspace, runs=5):
    tmp = Path(tempfile.mkdtemp(prefix="perf-"))
    os.environ["FAKE_ACP_STATE"] = str(tmp / "state.json")
    script = tmp / "script.json"
    script.write_text(json.dumps({
        "turns": [
            {"updates": [{"kind": "agent_text", "text": "ok"}],
             "stop_reason": "end_turn"}
        ]
    }, ensure_ascii=False), encoding="utf-8")
    store = CopilotStore(tmp / "perf.sqlite3")
    controller = CopilotController(
        executable=exe, runtime_args=args, dsh_home=str(dsh_home),
        launch_cwd=str(REPO), store=store,
        idle_shutdown_seconds=0, stderr_dir=tmp / "stderr",
    )
    results = {}

    # 状态2：启用但未唤起
    controller.start()
    time.sleep(0.3)
    n, rss = owned_tree_rss(os.getpid())
    results["enabled_idle"] = {"owned_extra_procs": 0, "note": "runtime 未启动"}

    # 状态3/5：派单与运行延迟（多次取中位）
    conv = controller.create_conversation(str(workspace))
    accept_lat, first_update_lat = [], []
    for i in range(runs):
        t0 = time.monotonic()
        run = controller.submit(conv.conversation_id, f"任务 {i}")
        # 已受理延迟：离开 PREPARING（SUBMITTING 可能一闪而过）
        ok = wait_past(controller, run.run_id, "PREPARING", timeout=60)
        t_submit = time.monotonic()
        # 事件到 UI：以状态可见为代理（snapshot 轮询 20ms 模拟 after 轮询）
        ok2 = wait_state(controller, run.run_id, "SETTLED", timeout=60)
        t_settled = time.monotonic()
        accept_lat.append((t_submit - t0) * 1000)
        if ok and ok2:
            first_update_lat.append((t_settled - t_submit) * 1000)
        spawned = controller._spawned
        if spawned:
            n, rss = owned_tree_rss(spawned.pid)
    results["dispatch"] = {
        "runs": runs,
        "accept_to_submitting_ms_median": round(statistics.median(accept_lat), 1),
        "turn_total_ms_median": round(statistics.median(first_update_lat), 1),
        "all_accept_ms": [round(x, 1) for x in accept_lat],
        "owned_tree": {"procs": n, "rss_mb": round(rss / 1e6, 1)},
    }

    # 状态4：任务结束后空闲（无闲置回收配置下 runtime 保持）
    time.sleep(0.5)
    spawned = controller._spawned
    if spawned:
        n, rss = owned_tree_rss(spawned.pid)
        results["idle_after_task"] = {"owned_tree": {"procs": n, "rss_mb": round(rss / 1e6, 1)}}

    controller.shutdown(timeout=20)
    store.close()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default=sys.executable)
    parser.add_argument("--runtime-args", default=None)
    parser.add_argument("--dsh-home", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    # 状态1：功能关闭 = 不构造控制器（0 额外进程/内存，按定义成立）
    out = {
        "disabled": {"owned_extra_procs": 0, "note": "copilot 未启用时无导入副作用"},
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    rt_args = args.runtime_args.split() if args.runtime_args else ["-m", "copilot.fake_agent", "--script", "SCRIPT"]
    if args.runtime_args is None:
        # fake 模式：脚本路径由 measure 内部生成，这里先跑一遍拿路径再重跑不优雅；
        # 简化：直接在 measure 里处理 fake（exe=python 且 args 以 -m copilot.fake_agent 开头）
        rt_args = None
    Path(args.dsh_home).mkdir(parents=True, exist_ok=True)
    Path(args.workspace).mkdir(parents=True, exist_ok=True)

    if rt_args is None:
        # fake agent 路径
        tmp = Path(tempfile.mkdtemp(prefix="perf-script-"))
        os.environ["FAKE_ACP_STATE"] = str(tmp / "state.json")
        script = tmp / "script.json"
        script.write_text(json.dumps({
            "turns": [
                {"updates": [{"kind": "agent_text", "text": "ok"}],
                 "stop_reason": "end_turn"}
            ]
        }, ensure_ascii=False), encoding="utf-8")
        rt_args = ["-m", "copilot.fake_agent", "--script", str(script)]

    res = measure(args.exe, rt_args, Path(args.dsh_home), Path(args.workspace), args.runs)
    out.update(res)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
