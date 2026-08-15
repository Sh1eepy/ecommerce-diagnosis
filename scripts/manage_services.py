"""服务管理：一键启动/停止/查看 API、Worker、调度器。

用法（配合根目录的 start_all.bat / stop_all.bat 双击使用）：
  python scripts/manage_services.py start          # 启动全部三个服务
  python scripts/manage_services.py start api      # 只启动 API
  python scripts/manage_services.py stop           # 停止全部
  python scripts/manage_services.py stop worker    # 只停止 Worker
  python scripts/manage_services.py status         # 查看状态

设计：
- 三个服务以后台子进程方式运行，日志写 logs/service/{name}.log（不弹黑窗）
- PID 记录在 logs/service/{name}.pid，stop 按 PID 整树终止
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVC_DIR = ROOT / "logs" / "service"

SERVICES = {
    "api": [
        sys.executable, "-c",
        "import uvicorn; uvicorn.run('app.api.main:app', host='127.0.0.1', port=8000, log_level='info')",
    ],
    "worker": [
        sys.executable, "-c",
        "import runpy; runpy.run_path('scripts/run_worker.py', run_name='__main__')",
    ],
    "scheduler": [
        sys.executable, "-c",
        "import runpy; runpy.run_path('scripts/run_scheduler.py', run_name='__main__')",
    ],
}


def _pid_file(name: str) -> Path:
    return SVC_DIR / f"{name}.pid"


def _read_pid(name: str) -> int | None:
    pf = _pid_file(name)
    if pf.exists():
        try:
            return int(pf.read_text(encoding="utf-8").strip())
        except ValueError:
            return None
    return None


def _is_running(pid: int) -> bool:
    """判断 PID 是否存活。Windows 用原生 API（不依赖 tasklist，受限环境也可靠）。"""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start(name: str = "all") -> None:
    names = list(SERVICES) if name == "all" else [name]
    SVC_DIR.mkdir(parents=True, exist_ok=True)
    for n in names:
        old_pid = _read_pid(n)
        if old_pid and _is_running(old_pid):
            print(f"[{n}] 已在运行 (PID {old_pid})")
            continue
        # 日志追加写，编码 utf-8，行缓冲（便于 tail 观察）
        logf = open(SVC_DIR / f"{n}.log", "a", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            SERVICES[n],
            cwd=str(ROOT),          # 保证 .env / logs 相对路径正确
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 不弹黑窗
        )
        _pid_file(n).write_text(str(proc.pid), encoding="utf-8")
        print(f"[{n}] 已启动 (PID {proc.pid})，日志 logs/service/{n}.log")
        time.sleep(1.5)  # 错开启动，避免端口/连接竞争


def stop(name: str = "all") -> None:
    names = list(SERVICES) if name == "all" else [name]
    for n in names:
        pid = _read_pid(n)
        if pid is None:
            print(f"[{n}] 未在运行")
            continue
        if _is_running(pid):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
            print(f"[{n}] 已停止 (PID {pid})")
        else:
            print(f"[{n}] 进程已不存在，清理残留 PID 文件")
        _pid_file(n).unlink(missing_ok=True)


def status() -> None:
    print("服务状态：")
    any_running = False
    for n in SERVICES:
        pid = _read_pid(n)
        if pid is None:
            print(f"  {n:10s} 未启动")
            continue
        if _is_running(pid):
            print(f"  {n:10s} 运行中  (PID {pid})")
            any_running = True
        else:
            print(f"  {n:10s} 已退出  (残留 PID {pid}，可执行 stop 清理)")
    if not any_running and all(_read_pid(n) is None for n in SERVICES):
        print("  （全部未启动）")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    name = args[1] if len(args) > 1 else "all"
    if name not in SERVICES and name != "all":
        print(f"未知服务: {name}，可选: {'/'.join(SERVICES)} 或 all")
        return
    if cmd == "start":
        start(name)
    elif cmd == "stop":
        stop(name)
    else:
        status()


if __name__ == "__main__":
    main()
