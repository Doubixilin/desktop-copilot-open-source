# -*- coding: utf-8 -*-
"""mock CLI agent：复刻 DSH headless NDJSON 事件流，供 GenericCliAdapter 测试。

用法（args 里恰含一个 {prompt}，由适配器替换）：
    program=sys.executable
    args=["mock_cli_agent.py", "--mode", "ok", "{prompt}"]

模式：
    ok     session/status/text/final 四事件，退出 0
    fail   text 事件后退出 3
    needs  输出"CONFIRM: 等待确认"行后退出 0（needs_input 正则过程态）
    sleep  心跳 text 后睡 120 秒（取消测试用）
    echo   纯文本模式回显 prompt，退出 0（{prompt} 替换断言用）
"""
import argparse
import json
import sys
import time


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok")
    ap.add_argument("prompt", nargs="*")
    args = ap.parse_args()
    prompt = " ".join(args.prompt)

    if args.mode == "ok":
        emit({"type": "session", "sessionId": "mock-s1"})
        emit({"type": "status", "status": "working"})
        emit({"type": "text", "text": "中间输出"})
        emit({"type": "final", "text": f"最终结果：{prompt}"})
        sys.exit(0)
    if args.mode == "fail":
        emit({"type": "text", "text": "出错了"})
        sys.exit(3)
    if args.mode == "needs":
        emit({"type": "text", "text": "分析中"})
        print("CONFIRM: 等待确认（mock）", flush=True)
        time.sleep(2)  # 过程态可观察窗口（等待输入的 agent 不会立刻退出）
        emit({"type": "final", "text": "按 needs 提示收尾"})
        sys.exit(0)
    if args.mode == "sleep":
        emit({"type": "text", "text": "长任务开始"})
        time.sleep(120)
        sys.exit(0)
    if args.mode == "echo":
        print(f"ECHO|{prompt}", flush=True)
        sys.exit(0)
    print(f"unknown mode: {args.mode}", file=sys.stderr, flush=True)
    sys.exit(2)


if __name__ == "__main__":
    main()
