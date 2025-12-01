# utils_scripts/linkedin_login.py
# -*- coding: utf-8 -*-
"""
Gestion de la session LinkedIn (réutilisation de session + login manuel simplifié).

Fonction principale :
    ensure_session(page, storage_state_path, user_agent, locale, timezone_id, ...)

Comportement :
- Vérifie d'abord si une session est déjà active en ouvrant /feed.
- Si non, va sur /login et attend soit :
    * que l'utilisateur appuie sur Entrée en console (mode par défaut "press_enter"),
    * soit jusqu'à un timeout (mode "timeout").
- Dès que la session est détectée (URL /feed), la session Playwright est
  sauvegardée dans `storage_state_path` (cookies + storage) pour réutilisation.

Exemple d'utilisation :
    logged, page = await ensure_session(
        page,
        storage_state_path=STORAGE_STATE,
        user_agent=USER_AGENT,
        locale="fr-FR",
        timezone_id="Europe/Paris",
        # wait_mode="timeout", login_timeout_s=300  # 
    )
    if not logged:
        raise RuntimeError("Session LinkedIn indisponible.")
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Tuple

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

# Imports projet (config + logging central)
from linkedin_scrapper.config import settings
from linkedin_scrapper.logging_setup import get_logger

logger = get_logger(__name__)


# =========================
# Helpers
# =========================

def _looks_logged_in(page: Page) -> bool:
    """Heuristique très simple : URL contient /feed."""
    return "linkedin.com/feed" in (page.url or "")


async def _wait_for_user_enter(prompt: str = "Appuie sur Entrée quand la connexion LinkedIn est faite...") -> None:
    """
    Attend une validation clavier coté console SANS bloquer l'event loop.
    (input() est bloquant : on le déporte dans un executor.)
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: input(prompt))


async def _save_storage_state_if_possible(page: Page, storage_state_path: str) -> None:
    """Sauvegarde la session Playwright (cookies + localStorage) si possible."""
    try:
        p = Path(storage_state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        await page.context.storage_state(path=str(p))
        logger.info("Session LinkedIn sauvegardée → %s", p)
    except Exception as e:
        logger.error(
            "Impossible de sauvegarder le storage_state (%s): %s",
            storage_state_path,
            e,
            exc_info=True,
        )


# =========================
# Public API
# =========================

async def ensure_session(
    page: Page,
    storage_state_path: str,
    user_agent: str,
    locale: str,
    timezone_id: str,
    *,
    wait_mode: str = "press_enter",    # "press_enter" (défaut) | "timeout"
    login_timeout_s: int = 300         # utilisé seulement si wait_mode="timeout"
) -> Tuple[bool, Page]:
    """
    Garantit une session LinkedIn pour `page`.

    1) Tente d'accéder à /feed : si déjà connecté → succès immédiat.
    2) Sinon va sur /login et attend selon `wait_mode` :
        - "press_enter" : l'utilisateur se connecte manuellement dans la fenêtre,
          puis appuie sur Entrée dans la console. Vérification qu'on est bien sur /feed.
        - "timeout" : attend jusqu'à `login_timeout_s` secondes que l'URL corresponde à /feed.

    En cas de succès, sauvegarde le storage_state dans `storage_state_path`.

    Retour:
        (logged: bool, page: Page)
    """
    # 1) /feed rapide
    try:
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("load", timeout=settings.navigation_timeout_ms)
        except PlaywrightTimeoutError:
            # On tolère que la page n'atteigne pas "load" complet
            pass
        if _looks_logged_in(page):
            # Session active → on sauvegarde quand même pour rafraichir
            await _save_storage_state_if_possible(page, storage_state_path)
            return True, page
    except Exception:
        # On passera par le flux /login ci-dessous
        logger.warning("Accès direct à /feed indisponible, tentative via /login", exc_info=True)

    # 2) Pas connecté → /login
    try:
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    except Exception:
        # si échec d'accès direct à /login, on retentera via feed ensuite
        logger.warning("Echec d'accès direct à /login", exc_info=True)

    if wait_mode == "press_enter":
        # === Mode manuel : l'utilisateur a le temps qu'il veut ===
        logger.info("Connexion LinkedIn requise (mode manuel).")
        logger.info("Fais le login dans la fenêtre (mdp, 2FA, captcha si besoin).")

        while True:
            # invite utilisateur en console (affichage + attente)
            await _wait_for_user_enter("→ Quand tu vois le fil (/feed), reviens ici et appuie sur Entrée.\n")
            try:
                # cas fréquent : on est encore sur /login → on laisse une chance à la redirection rapide
                if not _looks_logged_in(page):
                    try:
                        await page.wait_for_url(
                            re.compile(r"https://www\.linkedin\.com/(feed/|feed\?|$)"),
                            timeout=settings.navigation_timeout_ms
                        )
                    except PlaywrightTimeoutError:
                        # rien, on revérifie juste après
                        pass

                if _looks_logged_in(page):
                    await _save_storage_state_if_possible(page, storage_state_path)
                    return True, page

                logger.warning("Pas encore sur /feed. Termine la connexion dans la fenêtre, puis ré-appuie sur Entrée.")
            except Exception as e:
                logger.error("Vérification de session: %s. Ré-appuie sur Entrée quand prêt.", e, exc_info=True)

    else:
        # === Mode timeout : on attend jusqu'à N secondes ===
        logger.info("Connexion LinkedIn requise (mode timeout). Attente max ~%ss", login_timeout_s)
        deadline = time.time() + login_timeout_s
        post_login_regex = re.compile(r"https://www\.linkedin\.com/(feed/|feed\?|$)")

        while time.time() < deadline:
            if _looks_logged_in(page):
                await _save_storage_state_if_possible(page, storage_state_path)
                return True, page
            try:
                await page.wait_for_url(post_login_regex, timeout=settings.navigation_timeout_ms)
                await _save_storage_state_if_possible(page, storage_state_path)
                return True, page
            except PlaywrightTimeoutError:
                await asyncio.sleep(1.0)

        logger.warning("Délai écoulé sans détection de connexion.")
        return False, page

    # Si on sort de la boucle (théoriquement jamais)
    return False, page



