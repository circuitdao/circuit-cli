"""
Progress handler factories for Circuit CLI.

Provides two functions:
- make_json_progress_handler(): returns a callable that writes JSONL events to stdout.
- make_text_progress_handler(): returns a callable that renders a spinner/timer to stderr
  and reacts to events emitted by CircuitRPCClient while waiting for confirmations.

These are extracted from inline definitions in circuit_rpc_cli.py to improve reuse and clarity.
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, Any


def make_json_progress_handler() -> Callable[[Dict[str, Any]], None]:
    """Return a simple JSONL writer for progress events (to stdout)."""
    import json

    def _json_progress(ev: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(ev) + "\n")
        sys.stdout.flush()

    return _json_progress


def make_text_progress_handler() -> Callable[[Dict[str, Any]], None]:
    """Return a text progress handler with spinner and elapsed time (to stderr).

    The handler maintains state per tx_id (or a generic key) and reacts to events:
    - poll, sleep: update status and ensure a ticker task is running
    - confirmed, failed, done, skipped, error: stop ticker and print final line
    """
    import asyncio
    import time

    # Internal state and resources captured by closure
    state: Dict[str, Dict[str, Any]] = {}
    spinner = ["⠋", "⠙", "⠚", "⠞", "⠖", "⠦", "⠴", "⠲", "⠳", "⠓"]

    try:
        is_tty = sys.stderr.isatty()
    except Exception:
        is_tty = False

    colors = {
        "ok": "\x1b[32m" if is_tty else "",
        "warn": "\x1b[33m" if is_tty else "",
        "err": "\x1b[31m" if is_tty else "",
        "info": "\x1b[36m" if is_tty else "",
        "dim": "\x1b[2m" if is_tty else "",
        "bold": "\x1b[1m" if is_tty else "",
        "reset": "\x1b[0m" if is_tty else "",
    }

    def elapsed_str(st: Dict[str, Any]) -> str:
        elapsed = int(time.time() - st["start"])
        mm = elapsed // 60
        ss = elapsed % 60
        return f"{mm:02d}:{ss:02d}"

    def write_line(st: Dict[str, Any], line: str, final: bool = False) -> None:
        last_len = st.get("last_len", 0)
        if final:
            clear = "" if last_len <= len(line) else " " * (last_len - len(line))
            sys.stderr.write("\r" + line + clear + "\n\n")
            st["last_len"] = 0
        else:
            sys.stderr.write("\r" + line)
            st["last_len"] = len(line)
        sys.stderr.flush()

    async def ticker(key: str, st: Dict[str, Any], txid: str | None):
        while not st["stopped"]:
            spin = spinner[st["spin_idx"] % len(spinner)]
            st["spin_idx"] += 1
            status = st.get("status")
            spin_col = f"{colors['info']}{spin}{colors['reset']}" if colors["info"] else spin
            status_col = f"{colors['info']}{status}{colors['reset']}" if colors["info"] else status
            elapsed_col = f"{colors['dim']}{elapsed_str(st)} {colors['reset']}" if colors["dim"] else elapsed_str(st)
            if txid:
                line = f"{spin_col} Waiting for tx {colors['bold']}{txid}{colors['reset']} | status: {status_col} | elapsed: {elapsed_col}"
            else:
                line = f"{spin_col} Waiting | status: {status_col} | elapsed: {elapsed_col}"
            write_line(st, line, final=False)
            try:
                await asyncio.sleep(0.1)
            except Exception:
                break

    def handler(ev: Dict[str, Any]) -> None:
        event = ev.get("event")
        txid = ev.get("tx_id")
        key = txid or "blocks_wait"
        now = time.time()
        st = state.get(key)
        if st is None:
            st = {
                "start": now,
                "status": "waiting",
                "stopped": False,
                "spin_idx": 0,
                "task": None,
            }
            state[key] = st

        if event == "poll":
            st["status"] = ev.get("status") or "pending"
            if st["task"] is None or st["task"].done():
                try:
                    loop = asyncio.get_running_loop()
                    st["task"] = loop.create_task(ticker(key, st, txid))
                except RuntimeError:
                    pass
        elif event == "sleep":
            rem = ev.get("remaining_blocks")
            st["status"] = f"{rem} block(s) remaining"
            if st["task"] is None or st["task"].done():
                try:
                    loop = asyncio.get_running_loop()
                    st["task"] = loop.create_task(ticker(key, st, txid))
                except RuntimeError:
                    pass
        elif event in ("confirmed", "failed"):
            st["stopped"] = True
            try:
                if st.get("task") is not None and not st["task"].done():
                    st["task"].cancel()
            except Exception:
                pass
            if event == "confirmed":
                outcome = f"{colors['ok']}CONFIRMED{colors['reset']}" if colors["ok"] else "CONFIRMED"
                check = f"{colors['ok']}✔{colors['reset']}" if colors["ok"] else "✔"
            else:
                outcome = f"{colors['err']}FAILED{colors['reset']}" if colors["err"] else "FAILED"
                check = f"{colors['err']}✖{colors['reset']}" if colors["err"] else "✖"
            total_col = f"{colors['dim']}{elapsed_str(st)}{colors['reset']}" if colors["dim"] else elapsed_str(st)
            write_line(st, f"{check} Transaction {colors['bold']}{txid}{colors['reset']} {outcome} | total time: {total_col}", final=True)
            state.pop(key, None)
        elif event == "done":
            st["stopped"] = True
            try:
                if st.get("task") is not None and not st["task"].done():
                    st["task"].cancel()
            except Exception:
                pass
            total_col = f"{colors['dim']}{elapsed_str(st)}{colors['reset']}" if colors["dim"] else elapsed_str(st)
            write_line(st, f"✓ Done | total time: {total_col}", final=True)
            state.pop(key, None)
        elif event == "skipped":
            st["stopped"] = True
            try:
                if st.get("task") is not None and not st["task"].done():
                    st["task"].cancel()
            except Exception:
                pass
            reason = ev.get("reason")
            write_line(st, f"⟲ Skipped waiting: {reason}", final=True)
            state.pop(key, None)
        elif event == "error":
            st["stopped"] = True
            try:
                if st.get("task") is not None and not st["task"].done():
                    st["task"].cancel()
            except Exception:
                pass
            code = ev.get("status_code")
            content = ev.get("content")
            total_col = f"{colors['dim']}{elapsed_str(st)}{colors['reset']}" if colors["dim"] else elapsed_str(st)
            write_line(st, f"{colors['err']}Error{colors['reset']} while checking status (code {code}): {content} | elapsed: {total_col}", final=True)
            state.pop(key, None)
        else:
            st["stopped"] = True
            try:
                if st.get("task") is not None and not st["task"].done():
                    st["task"].cancel()
            except Exception:
                pass
            write_line(st, str(ev), final=True)
            state.pop(key, None)

    return handler
