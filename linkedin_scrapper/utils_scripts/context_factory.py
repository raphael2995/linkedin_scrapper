# -*- coding: utf-8 -*-
"""
Fabrique de contexte Playwright pour scraper LinkedIn en limitant la détection :
- Paramètres navigateur cohérents (UA, viewport, locale, timezone, headers).
- Réutilisation optionnelle d'un storage_state (cookies/session).
- Injection facultative d'un petit script stealth (stealth.min.js) avant chargement.
"""

from __future__ import annotations

import os
from importlib.resources import files
from typing import Optional, Dict, Tuple

from playwright.async_api import async_playwright, Browser, BrowserContext

from linkedin_scrapper.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _headers() -> Dict[str, str]:
    return {"Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"}


async def launch_browser(headless: bool = False) -> Tuple:
    """
    Lance Chromium (canal Chrome stable pour une meilleure tolérance).
    Retourne (playwright, browser) ; à fermer plus tard par caller.
    """
    pw = await async_playwright().start()
    # channel="chrome" -> binaire Chrome stable
    browser = await pw.chromium.launch(headless=headless, channel="chrome")
    return pw, browser


async def new_context(
    browser: Browser,
    storage_state_path: Optional[str],
    proxy: Optional[Dict] = None,
    *,
    inject_stealth: bool = True,
    stealth_resource: str = "stealth.min.js",
) -> BrowserContext:
    """
    Crée un BrowserContext configuré et injecte (optionnellement) le script stealth
    AVANT tout chargement de page, pour qu'il s'applique à tous les onglets.

    - storage_state_path : chemin vers un JSON de session (facultatif)
    - proxy : dict Playwright (facultatif)
    - inject_stealth : True pour injecter linkedin_scrapper/stealth.min.js
    - stealth_resource : nom du fichier ressource dans le package
    """
    context = await browser.new_context(
        user_agent=DEFAULT_UA,
        viewport={"width": 1536, "height": 864},
        device_scale_factor=1.25,
        locale="fr-FR",
        timezone_id="Europe/Paris",
        extra_http_headers=_headers(),
        proxy=proxy if proxy else None,
        storage_state=(
            storage_state_path
            if (storage_state_path and os.path.exists(storage_state_path))
            else None
        ),
    )

    if inject_stealth:
        try:
            # Charge la ressource depuis le package installé (peu importe le CWD)
            with files("linkedin_scrapper").joinpath(stealth_resource).open(
                "r", encoding="utf-8"
            ) as f:
                stealth_js = f.read()
            # Injection au niveau CONTEXT pour s'appliquer à toutes les pages
            await context.add_init_script(stealth_js)
            logger.debug("Script stealth injecté depuis le package: %s", stealth_resource)
        except Exception as e:  # nosec - best effort
            # On ne bloque pas le pipeline si le stealth manque
            logger.warning(
                "Impossible d'injecter le script stealth (%s) : %s",
                stealth_resource,
                e,
                exc_info=True,
            )

    return context
