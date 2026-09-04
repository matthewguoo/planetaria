"""The server administration window's data: one summary (process, stores,
feed, broker, enforcer, account), the engine's recent journal across
every plan, and the tail of its own log file. The broker/data call feed is
/api/monitor/calls. Read-only; mounted on both servers."""

from __future__ import annotations

import asyncio
import os
import platform
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.services.call_log import CALL_LOG
from app.services.system_state import system_state

router = APIRouter(prefix="/api", tags=["admin"])

_STARTED = time.time()
_CPU_LAST: tuple[float, float] | None = None  # (wall, cpu seconds)


def _git_head(repo: Path) -> str | None:
    """Short SHA of the checkout, without shelling out."""
    try:
        head = (repo / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = repo / ".git" / head[5:]
            if ref.exists():
                return ref.read_text().strip()[:7]
            packed = repo / ".git" / "packed-refs"
            if packed.exists():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + head[5:]):
                        return line.split()[0][:7]
            return None
        return head[:7]
    except OSError:
        return None


def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    try:
        import resource

        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(kb / 1024, 1) if platform.system() != "Darwin" else round(kb / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


def _cpu_pct() -> float | None:
    """Process CPU% since the previous summary call (first call: since boot)."""
    global _CPU_LAST
    t = os.times()
    cpu = t.user + t.system
    now = time.monotonic()
    if _CPU_LAST is None:
        _CPU_LAST = (now, cpu)
        wall = now - (_STARTED_MONO)
        return round(cpu / wall * 100, 1) if wall > 0 else None
    wall, prev = now - _CPU_LAST[0], cpu - _CPU_LAST[1]
    _CPU_LAST = (now, cpu)
    return round(prev / wall * 100, 1) if wall > 0.2 else None


_STARTED_MONO = time.monotonic()


def log_file_path() -> Path | None:
    """Where main._attach_file_logging writes (mirrors its rule)."""
    configured = os.environ.get("PLANETARIA_LOG_DIR", "")
    if configured.lower() == "off":
        return None
    base = configured or os.path.join(os.environ.get("LOCALAPPDATA", "."), "planetaria-logs")
    return Path(base) / "engine.log"


def tail_lines(path: Path, lines: int, max_bytes: int = 256_000) -> list[str]:
    """Last `lines` lines of a text file, reading only its tail."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    with path.open("rb") as fh:
        fh.seek(max(size - max_bytes, 0))
        chunk = fh.read().decode("utf-8", errors="replace")
    out = chunk.splitlines()
    if size > max_bytes and out:
        out = out[1:]  # the first line is a torn fragment
    return out[-lines:]


@router.get("/admin/summary")
async def admin_summary(request: Request) -> dict:
    st = request.app.state
    settings = st.settings
    try:
        system = await system_state(st)
    except Exception as exc:  # noqa: BLE001
        system = {"error": str(exc)}
    account: dict = {}
    try:
        account = await st.trade.get_account()
    except Exception as exc:  # noqa: BLE001
        account = {"error": str(exc)[:200]}
    positions = 0
    try:
        positions = len(await st.risk.open_plans())
    except Exception:  # noqa: BLE001
        pass
    calls = CALL_LOG.status()
    last_error = next((c for c in CALL_LOG.recent(None, 300) if not c["ok"]), None)
    repo = Path(__file__).resolve().parents[3]
    caps = getattr(st, "capabilities", None)
    return {
        "server": {
            "mode": settings.trading_mode,
            "paper": settings.alpaca_paper,
            "account": getattr(settings, "alpaca_account_name", None),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "port": int(os.environ.get("PLANETARIA_PORT", 0)) or None,
            "started_at": datetime.fromtimestamp(_STARTED, tz=timezone.utc).isoformat(),
            "uptime_s": round(time.time() - _STARTED),
            "version": _git_head(repo),
            "python": platform.python_version(),
            "log_file": str(log_file_path()) if log_file_path() else None,
        },
        "process": {
            "cpu_pct": _cpu_pct(),
            "rss_mb": _rss_mb(),
            "tasks": len(asyncio.all_tasks()),
            "threads": len(os.listdir("/proc/self/task")) if os.path.isdir("/proc/self/task") else None,
        },
        "account": account,
        "open_plans": positions,
        "calls": calls,
        "last_error": last_error,
        "capabilities": caps.summary() if caps is not None else None,
        "system": system,
    }


@router.get("/admin/events")
async def admin_events(request: Request, limit: int = 60) -> dict:
    return {"events": await request.app.state.trade.fsm.events_recent(min(limit, 300))}


@router.get("/admin/log")
async def admin_log(lines: int = 80) -> dict:
    path = log_file_path()
    if path is None:
        return {"path": None, "lines": []}
    if not path.exists():
        raise HTTPException(404, f"no log file at {path}")
    return {"path": str(path), "lines": tail_lines(path, min(lines, 500))}
