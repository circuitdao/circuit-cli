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
from typing import Any, Callable, Dict


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
            sys.stderr.write("\r" + line + clear + "\n")
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
        elif event in ("started", "status", "state_fetched", "bids_completed", "auctions_started", "bad_debts_recovered", "completed", "waiting", "rpc_request", "transaction_push", "transaction_starting", "transaction_completed", "transaction_failed", 
                       "dexie_upload_started", "dexie_upload_request", "dexie_upload_success", "dexie_upload_failed",
                       "offer_renewal_started", "offer_renewal_attempt", "offer_renewal_success", "offer_renewal_failed",
                       "coin_splitting_skipped", "coin_splitting_started", "coin_splitting", "coin_split_success", "coin_split_failed", "coin_split_error", "coin_splitting_error",
                       "liquidator_started", "keys_loaded", "warning", "error", "current_balance", "balance_check_failed",
                       "offer_creation_started", "offer_creation_success", "offer_creation_partial_success", "offer_creation_failed", "offer_file_summary",
                       "debt_recovery_plan", "debt_recovery_skipped", "debt_recovery_starting", "debt_recovery_completed", "debt_recovery_failed", "debt_recovery_summary"):
            # Handle liquidator-specific events with user-friendly formatting
            message = ev.get("message", "")
            
            if event == "started":
                icon = f"{colors['info']}🚀{colors['reset']}" if colors["info"] else "🚀"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "status":
                icon = f"{colors['info']}ℹ️{colors['reset']}" if colors["info"] else "ℹ️"
                write_line(st, f"{icon} {message}", final=False)
            elif event == "state_fetched":
                icon = f"{colors['ok']}📊{colors['reset']}" if colors["ok"] else "📊"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "bids_completed":
                icon = f"{colors['ok']}💰{colors['reset']}" if colors["ok"] else "💰"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "auctions_started":
                icon = f"{colors['ok']}⚡{colors['reset']}" if colors["ok"] else "⚡"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "bad_debts_recovered":
                icon = f"{colors['ok']}🔧{colors['reset']}" if colors["ok"] else "🔧"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "completed":
                icon = f"{colors['ok']}✅{colors['reset']}" if colors["ok"] else "✅"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "waiting":
                icon = f"{colors['dim']}⏳{colors['reset']}" if colors["dim"] else "⏳"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "rpc_request":
                method = ev.get("method", "")
                endpoint = ev.get("endpoint", "")
                icon = f"{colors['dim']}→{colors['reset']}" if colors["dim"] else "→"
                write_line(st, f"{icon} {method} {endpoint}", final=True)
            elif event == "transaction_push":
                tx_type = ev.get("transaction_type", "")
                tx_id = ev.get("tx_id", "")[:8] + "..." if ev.get("tx_id") else ""
                icon = f"{colors['warn']}📤{colors['reset']}" if colors["warn"] else "📤"
                type_str = f" ({tx_type})" if tx_type else ""
                write_line(st, f"{icon} Pushing transaction{type_str} {tx_id}", final=True)
            elif event == "transaction_starting":
                icon = f"{colors['warn']}⚡{colors['reset']}" if colors["warn"] else "⚡"
                write_line(st, f"{icon} {message}", final=False)
            elif event == "transaction_completed":
                icon = f"{colors['ok']}✅{colors['reset']}" if colors["ok"] else "✅"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "transaction_failed":
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            # Dexie upload events
            elif event == "dexie_upload_started":
                icon = f"{colors['info']}📤{colors['reset']}" if colors["info"] else "📤"
                write_line(st, f"{icon} {message}", final=False)
            elif event == "dexie_upload_request":
                icon = f"{colors['dim']}→{colors['reset']}" if colors["dim"] else "→"
                write_line(st, f"{icon} {message}", final=False)
            elif event == "dexie_upload_success":
                icon = f"{colors['ok']}✅{colors['reset']}" if colors["ok"] else "✅"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "dexie_upload_failed":
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            # Offer renewal events
            elif event == "offer_renewal_started":
                icon = f"{colors['info']}🔄{colors['reset']}" if colors["info"] else "🔄"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "offer_renewal_attempt":
                icon = f"{colors['info']}🔁{colors['reset']}" if colors["info"] else "🔁"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "offer_renewal_success":
                icon = f"{colors['ok']}✅{colors['reset']}" if colors["ok"] else "✅"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "offer_renewal_failed":
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            # Coin splitting events
            elif event == "coin_splitting_skipped":
                icon = f"{colors['dim']}⏭️{colors['reset']}" if colors["dim"] else "⏭️"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "coin_splitting_started":
                icon = f"{colors['info']}✂️{colors['reset']}" if colors["info"] else "✂️"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "coin_splitting":
                icon = f"{colors['warn']}✂️{colors['reset']}" if colors["warn"] else "✂️"
                write_line(st, f"{icon} {message}", final=False)
            elif event == "coin_split_success":
                icon = f"{colors['ok']}✅{colors['reset']}" if colors["ok"] else "✅"
                write_line(st, f"{icon} {message}", final=True)
            elif event in ("coin_split_failed", "coin_split_error", "coin_splitting_error"):
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            # Liquidator startup events
            elif event == "liquidator_started":
                icon = f"{colors['ok']}🚀{colors['reset']}" if colors["ok"] else "🚀"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "keys_loaded":
                icon = f"{colors['ok']}🔑{colors['reset']}" if colors["ok"] else "🔑"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "warning":
                icon = f"{colors['warn']}⚠️{colors['reset']}" if colors["warn"] else "⚠️"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "error":
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            # Balance reporting events
            elif event == "current_balance":
                icon = f"{colors['info']}💰{colors['reset']}" if colors["info"] else "💰"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "balance_check_failed":
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            # Offer creation events
            elif event == "offer_creation_started":
                icon = f"{colors['info']}📝{colors['reset']}" if colors["info"] else "📝"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "offer_creation_success":
                icon = f"{colors['ok']}✅{colors['reset']}" if colors["ok"] else "✅"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "offer_creation_partial_success":
                icon = f"{colors['warn']}⚠️{colors['reset']}" if colors["warn"] else "⚠️"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "offer_creation_failed":
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "offer_file_summary":
                icon = f"{colors['info']}📄{colors['reset']}" if colors["info"] else "📄"
                write_line(st, f"{icon} {message}", final=True)
            # Debt recovery events
            elif event == "debt_recovery_plan":
                icon = f"{colors['info']}📋{colors['reset']}" if colors["info"] else "📋"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "debt_recovery_skipped":
                icon = f"{colors['dim']}⏭️{colors['reset']}" if colors["dim"] else "⏭️"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "debt_recovery_starting":
                icon = f"{colors['warn']}🔧{colors['reset']}" if colors["warn"] else "🔧"
                write_line(st, f"{icon} {message}", final=False)
            elif event == "debt_recovery_completed":
                icon = f"{colors['ok']}✅{colors['reset']}" if colors["ok"] else "✅"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "debt_recovery_failed":
                icon = f"{colors['err']}❌{colors['reset']}" if colors["err"] else "❌"
                write_line(st, f"{icon} {message}", final=True)
            elif event == "debt_recovery_summary":
                icon = f"{colors['info']}📊{colors['reset']}" if colors["info"] else "📊"
                write_line(st, f"{icon} {message}", final=True)
        else:
            # Handle any unknown events with user-friendly formatting instead of raw JSON
            st["stopped"] = True
            try:
                if st.get("task") is not None and not st["task"].done():
                    st["task"].cancel()
            except Exception:
                pass
            
            # Format unknown events in a user-friendly way
            event_name = ev.get("event", "unknown")
            message = ev.get("message", f"Event: {event_name}")
            icon = f"{colors['info']}ℹ️{colors['reset']}" if colors["info"] else "ℹ️"
            write_line(st, f"{icon} {message}", final=True)
            state.pop(key, None)

    return handler
