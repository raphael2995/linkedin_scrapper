# -*- coding: utf-8 -*-
"""
Logging centralisé (console + fichier), format homogène, encodage UTF-8.
Commentaires en français.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Union


def _force_utf8_stdio() -> None:
    """
    Force l'UTF-8 pour la console quand c'est possible (utile sous Windows).
    N'affecte pas les environnements où ce n'est pas supporté.
    """
    for stream_name in ("stdin", "stdout", "stderr"):
        try:
            stream = getattr(sys, stream_name)
            # Disponible depuis Python 3.7
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            # On ne casse pas le démarrage du logger pour ça.
            pass


def setup_logging(level: Union[int, str] = logging.INFO) -> None:
    """
    Configure le logging pour l'application (idempotent).
    - Console en UTF-8 si possible
    - Handler console + handler fichier (data/logs/app.log) en UTF-8
    - Format standard: "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    """
    if getattr(setup_logging, "_configured", False):
        return

    _force_utf8_stdio()

    # Permettre le réglage fin via variable d'env si souhaité (ex: "DEBUG", "INFO", "WARNING")
    env_level = os.getenv("LOG_LEVEL")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    if env_level:
        level = getattr(logging, env_level.upper(), level)

    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    root = logging.getLogger()
    root.setLevel(level)

    # --- Handler console (StreamHandler) ---
    sh = logging.StreamHandler()
    sh.setLevel(level)
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)

    # --- Handler fichier (FileHandler) ---
    # Dossier local au projet (évite de polluer d'autres emplacements)
    log_dir = Path("data") / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)
    except Exception:
        # Si on ne peut pas écrire le fichier de log, on continue avec la console uniquement.
        root.warning("Impossible d'initialiser le fichier de logs (data/logs/app.log).", exc_info=True)

    setup_logging._configured = True  # type: ignore[attr-defined]


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Retourne un logger prêt à l'emploi (et configure si nécessaire)."""
    setup_logging()
    return logging.getLogger(name or "app")
