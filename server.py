"""AGENT DECK 数据服务。

GET /performance -> {"psutil": {...}, "qoder": {...}}
GET /deck       -> {"sessions": [...], "history": {...}, "gauges": {...}, "ts": ...}
GET /usage      -> {"per_agent": {...}, "quota": {...}, ...}
界面每秒轮询一次。仅监听 127.0.0.1。

本地读取边界（审计 R05 / 复核 F01/F02）：Host 仅接受回环名（防 DNS
rebinding）；CORS 只回显本机/null 来源（file:// 的壁纸页面），外部网页
拿不到 ACAO 头、浏览器拦截读取。敏感内容字段（会话标题/项目/任务/
消息预览）**默认裁剪**，仅带有效令牌（?token=）返回：令牌依次取环境
变量 QD_API_TOKEN、%LOCALAPPDATA%/qoder-deck/api-token（缺失时自动
生成并落盘）；读取/生成失败一律视为无凭据、维持裁剪。请求日志只记
路由路径，不落查询参数。
"""
import atexit
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psutil

import agent_sessions
import search_panel
import usage
import usage_log
import usage_score
import zones_watcher

HOST = "127.0.0.1"
# 开发/验收时可换端口并行跑第二实例（watchdog 不设此变量，恒为 5000）
PORT = int(os.environ.get("QD_PORT", "5000"))
GPU_CACHE_TTL = 3.0
QODER_CACHE_TTL = 2.0
DECK_CACHE_TTL = 1.0
USAGE_CACHE_TTL = 60.0
TODAY_CACHE_TTL = 300.0   # 滴答 5 分钟轮询（对 API 配额友好）
TODAY_FAIL_TTL = 60.0     # 失败负缓存（未配置 token / 网络故障不每 2s 重试）
HISTORY_LEN = 300
QODER_ROOT = Path.home() / ".qoder-cn"
RUNNING_WINDOW = agent_sessions.RUNNING_WINDOW
ACTIVE_WINDOW = agent_sessions.ACTIVE_WINDOW
SESSION_ROOTS = {
    "qoder": QODER_ROOT,
    "hermes": Path.home() / "AppData" / "Local" / "hermes",
    "zcode": Path.home() / ".zcode",
    "kimicode": Path.home() / ".kimi-code",
    "kimiwork": Path.home() / "AppData" / "Roaming" / "kimi-desktop" / "kimi-agent",
    "codex": Path.home() / ".codex",
    "dsh": Path.home() / ".dsh",
}

class _TtlCache:
    """带锁 TTL 缓存：锁内双重检查，过期瞬间的并发请求只触发一次真实采集。

    审计 B10/N03：此前四个模块级裸 dict 无任何并发保护。每缓存独立锁
    （deck 的重建会嵌套调 gpu/qoder 的缓存，全局锁会自锁死）。
    """

    def __init__(self, ttl, init=None):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.ts = 0.0
        self.data = init

    def get(self, build):
        now = time.monotonic()
        with self.lock:
            if self.data is not None and now - self.ts < self.ttl:
                return self.data
            self.data = build()
            self.ts = now
            return self.data


_gpu_cache = _TtlCache(GPU_CACHE_TTL, init={})
_qoder_cache = _TtlCache(QODER_CACHE_TTL)
_deck_cache = _TtlCache(DECK_CACHE_TTL)
_usage_cache = _TtlCache(USAGE_CACHE_TTL)
_net_prev = None


# ---- 滴答清单 TODAY 块（官方 MCP，fused-desktop 票 07） ----
# 单飞 + 负缓存：HUD 每 2s 轮询 /copilot/hud，MCP 拉取最多 5 分钟一次，
# 失败（含未配置 token）60s 内不重试。一切失败 → 段缺席（页面回退演示数据）。

_today_lock = threading.Lock()
_today_state = {"payload": None, "ts": 0.0, "next_retry": 0.0, "building": False}


def _build_today():
    import dida_mcp

    token = dida_mcp.load_token()
    if not token:
        return None
    snap = dida_mcp.today_snapshot(dida_mcp.DidaClient(token))
    if not snap.get("ok"):
        return None
    return {k: snap[k] for k in ("tasks", "events", "fetched_at")}


def today_payload():
    """今日待办+24h 日程快照；未配置/失败 → None（调用方静默省略）。"""
    now = time.monotonic()
    with _today_lock:
        st = _today_state
        fresh = st["payload"] is not None and now - st["ts"] < TODAY_CACHE_TTL
        if fresh or st["building"] or now < st["next_retry"]:
            return st["payload"]
        st["building"] = True
    try:
        snap = _build_today()
    except Exception:  # 防御：MCP 链路任何异常都按"不可得"降级
        snap = None
    with _today_lock:
        st["building"] = False
        if snap is not None:
            st["payload"] = snap
            st["ts"] = time.monotonic()
        else:
            st["next_retry"] = time.monotonic() + TODAY_FAIL_TTL
    return snap if snap is not None else st["payload"]


def today_complete(project_id: str, task_id: str) -> dict:
    """勾选写回（complete_task）；成功后立即使缓存失效（下次轮询重拉）。"""
    import dida_mcp

    token = dida_mcp.load_token()
    if not token:
        return {"ok": False, "reason": "未配置 API 口令"}
    result = dida_mcp.complete_task(dida_mcp.DidaClient(token), project_id, task_id)
    if result.get("ok"):
        with _today_lock:
            _today_state["ts"] = 0.0
            _today_state["next_retry"] = 0.0
    return result
_cpu = {"percent": 0.0}
_history = {
    "cpu": deque(maxlen=HISTORY_LEN),
    "dl": deque(maxlen=HISTORY_LEN),
    "up": deque(maxlen=HISTORY_LEN),
    "gpu": deque(maxlen=HISTORY_LEN),
}


def _cpu_sampler():
    # psutil 的 cpu_percent(interval=None) 状态是线程本地的，
    # 而每个 HTTP 请求都在新线程中处理，因此固定在本线程内采样。
    prev_net = psutil.net_io_counters()
    prev_ts = time.monotonic()
    while True:
        percent = psutil.cpu_percent(interval=1)
        _cpu["percent"] = percent
        now = time.monotonic()
        cur_net = psutil.net_io_counters()
        dt = now - prev_ts
        dl = max(0.0, (cur_net.bytes_recv - prev_net.bytes_recv) / dt / 1024) if dt > 0 else 0.0
        up = max(0.0, (cur_net.bytes_sent - prev_net.bytes_sent) / dt / 1024) if dt > 0 else 0.0
        prev_net, prev_ts = cur_net, now
        gpu = gpu_metrics().get("gpu_usage")
        _history["cpu"].append(round(percent, 1))
        _history["dl"].append(round(dl, 1))
        _history["up"].append(round(up, 1))
        _history["gpu"].append(round(gpu, 1) if gpu is not None else None)


def gpu_metrics():
    def build():
        data = {}
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                # 父进程是 pythonw（无控制台）时，不压制子进程控制台会每 3 秒闪一个终端窗
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if out.returncode == 0 and out.stdout.strip():
                util, temp, mused, mtotal = (float(x) for x in out.stdout.strip().splitlines()[0].split(","))
                data = {
                    "gpu_usage": util,
                    "gpu_temp": temp,
                    "vram_usage": round(mused / mtotal * 100, 1) if mtotal else None,
                }
        except Exception:
            data = {}
        return data

    return _gpu_cache.get(build)


def net_metrics():
    global _net_prev
    c = psutil.net_io_counters()
    now = time.monotonic()
    result = {}
    if _net_prev is not None:
        prev_sent, prev_recv, prev_ts = _net_prev
        dt = now - prev_ts
        if dt > 0:
            result["download_speed"] = round(max(0.0, (c.bytes_recv - prev_recv) / dt / 1024), 1)
            result["upload_speed"] = round(max(0.0, (c.bytes_sent - prev_sent) / dt / 1024), 1)
    _net_prev = (c.bytes_sent, c.bytes_recv, now)
    return result


def qoder_state():
    def build():
        try:
            return _scan_qoder()
        except Exception:
            return {"active_sessions": 0, "session": None}

    return _qoder_cache.get(build)


def deck_state():
    def build():
        try:
            sessions = agent_sessions.collect_sessions(SESSION_ROOTS, time.time())
        except Exception:
            sessions = []
        # 副驾权威投影（06 票）：controller 在本进程内；禁用/未安装时
        # 懒导入失败即原样返回 scanner 结果，不影响旧路径
        try:
            from copilot import projection, runtime_host
            controller, _ = runtime_host.get_controller()
            snapshot = controller.snapshot() if controller is not None else None
            sessions = projection.project_deck(
                sessions, snapshot, time.time(),
                scanner_dsh_namespace=str(SESSION_ROOTS["dsh"]),
            )
        except Exception:
            pass
        return {
            "sessions": sessions,
            "history": {k: list(v) for k, v in _history.items()},
            "gauges": build_payload()["psutil"],
            "panel": search_panel.panel_rect(),
            "ts": time.time(),
        }

    return _deck_cache.get(build)


def usage_state():
    """GET /usage：各 agent 今日 token + 额度（fused-desktop 票 04）。60s 缓存。"""

    def build():
        try:
            data = usage.usage_state(SESSION_ROOTS)
        except Exception:
            data = {"per_agent": {}, "quota": {}, "day_start": None}
        return dict(data, ts=time.time())

    return _usage_cache.get(build)


def _scan_qoder():
    wall = time.time()
    projects_dir = QODER_ROOT / "projects"
    sessions = []
    if projects_dir.is_dir():
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            for jf in proj.glob("*.jsonl"):
                try:
                    mtime = jf.stat().st_mtime
                except OSError:
                    continue
                age = wall - mtime
                if age > ACTIVE_WINDOW:
                    continue
                sessions.append({"mtime": mtime, "age": age, "id": jf.stem, "path": jf})
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    result = {"active_sessions": len(sessions), "session": None}
    if not sessions:
        return result
    latest = sessions[0]
    done, total, current = agent_sessions.task_stats(latest["id"], QODER_ROOT)
    session = {
        "project": agent_sessions.project_name(latest["path"]),
        "running": latest["age"] <= RUNNING_WINDOW,
        "tasks_done": done,
        "tasks_total": total,
        "current_task": current,
    }
    result["session"] = session
    return result


def build_payload():
    mem = psutil.virtual_memory()
    data = {
        "cpu": _cpu["percent"],
        "memory": mem.percent,
        "memory_gb": f"{mem.used / 2**30:05.2f} GB/{mem.total / 2**30:05.2f} GB",
    }
    data.update(gpu_metrics())
    data.update(net_metrics())
    return {"psutil": data, "qoder": qoder_state()}


def _public_performance(payload):
    """无令牌视图：剥掉 qoder 会话的项目名与当前任务，保留计数与运行态。"""
    qoder = payload.get("qoder") or {}
    session = qoder.get("session")
    if isinstance(session, dict):
        qoder = dict(qoder, session={k: v for k, v in session.items()
                                     if k not in ("project", "current_task")})
    return dict(payload, qoder=qoder)


def _token_path():
    return Path(os.environ.get("LOCALAPPDATA", "")) / "qoder-deck" / "api-token"


def _configured_token():
    """访问令牌：环境变量 QD_API_TOKEN > api-token 文件；文件缺失时自动生成。

    读取/生成失败一律返回 None，调用方按"无有效令牌"处理——敏感字段维持
    裁剪，绝不回退到全量。已有文件存在但读不了/解码失败（复核 R2-02，
    UnicodeDecodeError ⊂ ValueError 不在 OSError 内）时**不静默覆盖**既有
    凭据：返回 None 等人工排查，避免异常击穿启动预热与请求处理。
    """
    env = os.environ.get("QD_API_TOKEN")
    if env and env.strip():
        return env.strip()
    path = _token_path()
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass  # 尚无令牌文件：走自动生成
    except (OSError, ValueError):
        return None  # 已有文件损坏/不可读：不覆盖，按无有效令牌处理
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(16)
        path.write_text(value, encoding="utf-8")
        return value
    except OSError:
        return None


def origin_allowed(origin):
    """CORS 回显白名单：file:// 页面的 null、回环名的 http(s) 来源。

    null 不是可信凭据（沙箱 iframe 也能产生 null）——敏感内容的真正
    闸门是令牌，这里只挡外部站点的普通跨源读取（审计 R05）。
    """
    if not origin:
        return False
    value = origin.strip().lower()
    if value == "null":
        return True
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            self.send_error(403, "non-local Host header")
            return
        parts = urlsplit(self.path)
        # 敏感内容默认裁剪（F01）：只有令牌校验通过才返回全量；未配置/
        # 读取失败同样视为无凭据。Origin 白名单只是 CORS 层，null 可被
        # 沙箱 iframe 伪造，不能替代令牌。
        token = _configured_token()
        supplied = (parse_qs(parts.query).get("token") or [""])[0]
        trusted = bool(token) and hmac.compare_digest(
            supplied.encode("utf-8"), token.encode("utf-8"))
        if parts.path == "/performance":
            payload = build_payload()
            if not trusted:
                payload = _public_performance(payload)
        elif parts.path == "/deck":
            # trusted 标志供前端区分"服务离线"与"令牌失效"（不泄露令牌本身）
            payload = dict(deck_state(), trusted=trusted)
            if not trusted:
                payload = dict(payload, sessions=[
                    {k: v for k, v in s.items()
                     if k not in ("title", "project", "preview", "preview_role",
                                  "tasks", "current_task")}
                    for s in payload["sessions"]])
        elif parts.path == "/copilot/hud":
            payload = self._copilot_hud(trusted)
        elif parts.path == "/copilot/hud/events":
            from copilot import hud_api, runtime_host

            controller, _ = runtime_host.get_controller()
            q = parse_qs(parts.query)
            payload = hud_api.list_events(
                controller,
                (q.get("conversation_id") or [""])[0],
                int((q.get("after_seq") or ["0"])[0] or 0),
            )
        elif parts.path == "/usage":
            payload = usage_state()
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        origin = self.headers.get("Origin")
        if origin is not None and origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code="-", size="-"):
        # 请求行可能带 ?token= 等敏感查询参数（F02）：日志只保留路由路径
        self.log_message('"%s %s" %s %s', self.command, self.path.split("?", 1)[0],
                         code, size)

    # ---- 任务中枢 HTTP 面（M5）：copilot/hud_api 纯函数 + 令牌纪律 ----

    def _copilot_hud(self, trusted: bool):
        from copilot import config as copilot_config, hud_api, runtime_host

        hud_api.set_data_dir(copilot_config.default_data_dir())
        controller, reason = runtime_host.get_controller()
        hub = runtime_host.get_hub() if controller is not None else None
        # 归档不再由 HUD GET 驱动（票03 B07）：结算作业线程负责，界面打开与否都归档
        payload = hud_api.hud_snapshot(controller, hub)
        payload["system"] = build_payload()["psutil"]
        if trusted:
            # 今日待办（滴答 MCP）：未配置/失败时段缺席，页面回退演示数据
            today = today_payload()
            if today is not None:
                payload["today"] = today
        if controller is None:
            payload["reason"] = reason or payload.get("reason") or "copilot 未启用"
        if not trusted:
            # 与 /deck 同纪律：未授信不留对话内容与许可详情
            payload["conversations"] = [
                {k: v for k, v in c.items() if k != "events"}
                for c in payload.get("conversations", [])
            ]
            payload["pending_permissions"] = []
            payload["tasks"] = {
                **payload.get("tasks", {}),
                "tasks": [
                    {k: v for k, v in t.items()
                     if k not in ("title", "last_text", "extra", "log_path")}
                    for t in payload.get("tasks", {}).get("tasks", [])
                ],
            }
        return payload

    def do_POST(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            self.send_error(403, "non-local Host header")
            return
        parts = urlsplit(self.path)
        token = _configured_token()
        supplied = (self.headers.get("X-QD-Token") or "")
        trusted = bool(token) and hmac.compare_digest(
            supplied.encode("utf-8"), token.encode("utf-8"))
        if parts.path not in ("/copilot/hud/submit", "/copilot/hud/permission",
                              "/copilot/hud/cancel", "/copilot/hud/feedback",
                              "/copilot/hud/today/complete"):
            self.send_error(404)
            return
        if not trusted:
            self._copilot_json({"error": {"code": "forbidden",
                                          "message": "缺少有效令牌"}}, 403)
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 128 * 1024)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, OSError):
            self._copilot_json({"error": {"code": "bad_json",
                                          "message": "请求体不是 JSON 对象"}}, 400)
            return
        from copilot import hud_api, runtime_host

        controller, _ = runtime_host.get_controller()
        data_dir = None
        try:
            from copilot import config as copilot_config

            data_dir = copilot_config.default_data_dir() / "scene_packages"
        except Exception:
            pass
        if parts.path == "/copilot/hud/submit":
            import os as _os

            result = hud_api.submit_task(
                controller, payload,
                default_workspace=_os.path.dirname(_os.path.abspath(__file__)),
                package_dir=data_dir,
            )
        elif parts.path == "/copilot/hud/permission":
            result = hud_api.answer_permission(controller, payload)
        elif parts.path == "/copilot/hud/feedback":
            from copilot import config as copilot_config

            result = hud_api.save_feedback(
                copilot_config.default_data_dir(), payload)
        elif parts.path == "/copilot/hud/today/complete":
            task_id = str(payload.get("task_id") or "")
            project_id = str(payload.get("project_id") or "")
            done = today_complete(project_id, task_id)
            self._copilot_json(done if done.get("ok") else
                               {"error": {"code": "today_complete_failed",
                                          "message": done.get("reason", "写回失败")}},
                               200 if done.get("ok") else 502)
            return
        else:
            result = hud_api.cancel_run(controller, payload)
        self._copilot_json(result, 400 if "error" in result else 200)

    def _copilot_json(self, payload, status: int):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 兜底脱敏：任何来源的日志行都不落 token 值
        line = re.sub(r"([?&]token=)[^ \"'&]+", r"\1<redacted>", fmt % args)
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {line}", flush=True)


def _usage_collector():
    """使用日志采集：独立线程、2s 一轮，不占用 1Hz 采样循环（见 spec 使用日志节）。"""
    try:
        collector = usage_log.Collector()
    except Exception as exc:
        print(f"usage-log: 初始化失败，采集停用：{exc}", flush=True)
        return
    usage_log.run_loop(collector)


def _zone_watcher():
    """桌面目录看门狗：新增/删除触发增量编排（见 zones_watcher 的漂移纠正语义）。"""
    try:
        zones_watcher.run_watch_loop()
    except Exception as exc:
        zones_watcher.log.exception("看门狗线程退出：%s", exc)


def _settlement_worker():
    """结算作业线程（票03 B07）：controller 终态通知驱动归档+启动补偿，
    不依赖 HUD 打开。副驾未启用时线程内静默等待，不影响其余服务。"""
    try:
        from copilot import config as copilot_config, runtime_host, settlement

        settlement.start_default(runtime_host.get_controller,
                                 copilot_config.default_data_dir)
    except Exception as exc:
        print(f"[settlement] 启动失败：{exc}", flush=True)


def _shutdown_copilot() -> None:
    """受控关闭副驾运行时（幂等、有界，R9）。

    不能依赖进程退出时 Job kill-on-close 兜底代替正常关闭：那会跳过
    取消在途轮次、许可过期、会话 close 与记录 flush。controller 尚未
    构造时是无操作，不触发副驾初始化。
    """
    try:
        from copilot import runtime_host, settlement

        settlement.shutdown_default()
        runtime_host.shutdown()
    except Exception:
        pass


def main():
    # Windows 的 SO_REUSEADDR 允许双进程同绑一端口，故用 connect 探测做单实例守卫
    with socket.socket() as probe:
        try:
            probe.connect((HOST, PORT))
            return
        except OSError:
            pass
    # 预热令牌文件（F01）：首次启动即生成，前端接线可立即取用；不打印值
    if _configured_token():
        print(f"api-token ready: {_token_path()}", flush=True)
    # 副驾受控关闭挂到进程退出钩子：main 的 finally 覆盖常规路径，atexit
    # 兜住绕过 main 的解释器退出（os._exit/硬杀由 Job 兜底，属另一层）
    atexit.register(_shutdown_copilot)
    threading.Thread(target=_cpu_sampler, daemon=True).start()
    threading.Thread(target=_usage_collector, daemon=True).start()
    threading.Thread(target=_zone_watcher, daemon=True).start()
    # 结算作业（票03）：终态轮次结算驱动归档——不开 HUD 也归档（B07/R07）
    threading.Thread(target=_settlement_worker, daemon=True).start()
    # 搜索面板窗口线程（ADR-0003）：能力在数据服务内；窗口崩溃被线程内
    # 兜住，不影响 /deck 轮询（验收：QD_PANEL_CRASH 注入崩溃后 /deck 仍 200）
    # HUD 模式（native_hud）下用 QD_PANEL=0 关掉旧 Tk 面板——任务中枢/副驾
    # 能力不受影响（HUD 经 /copilot/hud* 访问）。
    if os.environ.get("QD_PANEL", "1") != "0":
        search_panel.start_thread()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return  # 端口已被占用（另一实例在跑），静默退出
    print(f"serving http://{HOST}:{PORT}/performance  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 先停 HTTP（不再受理新派单），再做副驾的有界受控关闭（R9）
        try:
            server.server_close()
        except Exception:
            pass
        _shutdown_copilot()


if __name__ == "__main__":
    main()
