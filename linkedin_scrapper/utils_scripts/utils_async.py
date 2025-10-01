# -*- coding: utf-8 -*-
"""
Utilitaire simple et efficace pour exécuter une coroutine asyncio depuis du code
synchrone, compatible scripts et environnements interactifs (Jupyter/Spyder),
avec prise en charge fiable de Windows (sousâ€‘processus).

Usage
-----
>>> from utils_async import run_coro
>>> import asyncio
>>> async def main():
...     await asyncio.sleep(0.1)
...     return "ok"
>>> print(run_coro(main()))
"""
from __future__ import annotations

import sys
import asyncio
import threading
from typing import Any, Coroutine


def run_coro(coro: Coroutine[Any, Any, Any]) -> Any:
    """Exécute la coroutine *coro* et retourne son résultat.

    - S'il n'y a PAS de boucle en cours : utilise `asyncio.run`.
    - S'il y a DÃ‰JÃ€ une boucle (Jupyter/Spyder) : lance une boucle dédiée dans
      un thread séparé (Proactor sous Windows).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Pas de loop en cours → chemin simple
        _ensure_windows_proactor_policy()
        return asyncio.run(coro)

    # Une loop est déjà active → isoler dans un thread dédié
    out: dict[str, Any] = {"result": None, "error": None}

    def _runner() -> None:
        try:
            if sys.platform.startswith("win") and hasattr(asyncio, "ProactorEventLoop"):
                loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
            else:
                loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            out["result"] = loop.run_until_complete(coro)
        except BaseException as e:  # noqa: BLE001
            out["error"] = e
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_runner, daemon=True, name="run_coro_thread")
    t.start()
    t.join()

    if out["error"]:
        raise out["error"]
    return out["result"]


def _ensure_windows_proactor_policy() -> None:
    """Force la politique Proactor sous Windows pour fiabiliser les sousâ€‘processus."""
    if not sys.platform.startswith("win"):
        return
    policy_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_cls is not None:
        try:
            asyncio.set_event_loop_policy(policy_cls())  # type: ignore[call-arg]
        except Exception:
            # Environnements restreints : on ignore si on ne peut pas changer la politique.
            pass


__all__ = ["run_coro"]




