# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from importlib.resources import files
from linkedin_scrapper.logging_setup import get_logger

logger = get_logger(__name__)

# Emplacement historique (relatif au CWD) — gardé en fallback
CONFIG_PATH = Path("config/selectors.json")


def _load_json_robuste() -> Dict[str, Any]:
    """
    Charge selectors.json en essayant d'abord la ressource packagée
    linkedin_scrapper/config/selectors.json, puis en retombant sur
    config/selectors.json (chemin relatif au CWD).
    """
    # 1) depuis le package
    try:
        p = files("linkedin_scrapper.config").joinpath("selectors.json")
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        logger.debug("selectors.json chargé depuis le package: %s", p)
        return data
    except Exception as e:
        logger.debug("Échec lecture packagée: %s", e)

    # 2) fallback CWD
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        logger.debug("selectors.json chargé depuis le CWD: %s", CONFIG_PATH.resolve())
        return data
    except Exception as e:
        logger.warning("Impossible de charger selectors.json (%s)", e, exc_info=True)
        return {}


def load_selectors() -> Dict[str, Any]:
    """Retourne le JSON complet (linkedin + google)."""
    return _load_json_robuste()


def get_google_selectors() -> Dict[str, Any]:
    """Retourne le bloc 'google' du JSON."""
    return _load_json_robuste().get("google", {})


def get_linkedin_selectors(locale: Optional[str] = None) -> Dict[str, Any]:
    """
    Retourne le bloc LinkedIn pour une locale donnée (par défaut: locale_default puis 'fr').
    Structure: { "profile": {...}, "search": {...} }
    """
    root = _load_json_robuste().get("linkedin", {})
    if not root:
        return {}
    loc = locale or root.get("locale_default") or "fr"
    return {
        "profile": root.get("locales", {}).get(loc, {}).get("profile", {}),
        "search": root.get("locales", {}).get(loc, {}).get("search", {}),
    }


# ---------- Outils internes ----------
def _get_by_path(base: Dict[str, Any], parts: Iterable[str], default: Optional[Any] = None) -> Any:
    """Accès dict par chemin (parts). Renvoie default si manquant."""
    cur: Any = base
    for k in parts:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ---------- Rétro-compat : sel_list (API héritée) ----------
def sel_list(*path: str, default: Optional[List[str]] = None, locale: Optional[str] = None) -> List[str]:
    """
    API historique utilisée ailleurs dans le projet.
    Exemples acceptés :
      sel_list("google", "search", "input")
      sel_list("linkedin", "profile", "sections", "experience", "items")
      sel_list("linkedin.profile.sections.education.items")
    Gestion locale automatique pour linkedin.* via locale_default si non fournie.
    """
    if len(path) == 1 and isinstance(path[0], str) and "." in path[0]:
        parts = path[0].split(".")
    else:
        parts = list(path)

    data = _load_json_robuste()
    out: Any = None

    if not parts:
        return default or []

    # Branche LinkedIn: insère 'locales/<locale>' dans le chemin
    if parts[0] == "linkedin":
        root = data.get("linkedin", {})
        loc = locale or root.get("locale_default") or "fr"
        # Recompose: linkedin.locales.<loc>.{reste}
        rem = parts[1:]
        full = ["locales", loc] + rem
        out = _get_by_path(root, full, default=default)
    else:
        # google.* ou autre: parcours direct
        out = _get_by_path(data, parts, default=default)

    # Normalise en liste de str
    if out is None:
        return default or []
    if isinstance(out, list):
        return out
    # si c'est une chaîne simple par erreur, on la met en liste
    if isinstance(out, str):
        return [out]
    return default or []


# ---------- Rétro-compat : pick_locale & sel_dict ----------
def pick_locale(requested: Optional[str] = None) -> str:
    """
    Retourne la locale à utiliser pour LinkedIn :
    - si `requested` est fournie, on la retourne telle quelle,
    - sinon on prend linkedin.locale_default du selectors.json,
    - sinon 'fr' par défaut.
    """
    if requested:
        return requested
    root = _load_json_robuste().get("linkedin", {})
    return root.get("locale_default") or "fr"


def sel_dict(*path: str, locale: Optional[str] = None, default: Optional[dict] = None) -> dict:
    """
    Variante dict de sel_list :
      sel_dict("linkedin", "profile", locale="fr")
      sel_dict("google", "search")
      sel_dict("linkedin.profile.sections")
    """
    if len(path) == 1 and isinstance(path[0], str) and "." in path[0]:
        parts = path[0].split(".")
    else:
        parts = list(path)

    data = _load_json_robuste()
    if not parts:
        return default or {}

    if parts[0] == "linkedin":
        root = data.get("linkedin", {})
        loc = locale or root.get("locale_default") or "fr"
        # linkedin.locales.<loc>.<reste>
        rem = parts[1:]
        full = ["locales", loc] + rem
        out = _get_by_path(root, full, default=default or {})
        return out if isinstance(out, dict) else (default or {})
    else:
        out = _get_by_path(data, parts, default=default or {})
        return out if isinstance(out, dict) else (default or {})


__all__ = [
    "load_selectors",
    "get_google_selectors",
    "get_linkedin_selectors",
    "sel_list",
    "pick_locale",
    "sel_dict",
]
