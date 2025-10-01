# step2_profile_scraper/web_interactions.py
# -*- coding: utf-8 -*-
"""
Interactions â€œhumainesâ€ de navigation:
- frappe simulée
- scroll progressif
- simulation de petits mouvements sur la page
- ouverture d’un profil avec stabilisation du header
"""

from __future__ import annotations

import asyncio
import random
import math
from typing import Tuple
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from linkedin_scrapper.config import settings
from linkedin_scrapper.utils_scripts.selectors_registry import sel_list, pick_locale
from linkedin_scrapper.logging_setup import get_logger

logger = get_logger(__name__)


# --- Delais "humains" (loi log-normale) ---
def human_delay_ms(mean_ms: int = 600, sigma: float = 0.45) -> int:
    return int(math.exp(random.gauss(math.log(mean_ms), sigma)))

async def settle(page: Page, min_ms: int = 450, max_ms: int = 1200):
    # petite pause pour laisser le DOM se stabiliser
    await page.wait_for_timeout(random.randint(min_ms, max_ms))

async def _locale_hint(page: Page) -> str:
    """Déduit 'fr' / 'en' via navigator.language, mappé par pick_locale()."""
    try:
        lang = await page.evaluate("() => navigator.language || navigator.userLanguage || 'en-US'")
    except Exception:
        lang = "en-US"
    return pick_locale("linkedin", lang)

async def _first_clickable(scope, selectors, timeout=3000):
    """
    Retourne le premier locator visible + enabled parmi une liste de sélecteurs CSS, sinon None.
    `scope` peut Ãªtre `page` ou un locator parent.
    """
    for css in selectors or []:
        try:
            loc = scope.locator(css).first
            await loc.wait_for(state="visible", timeout=timeout)
            if await loc.is_enabled():
                return loc
        except Exception:
            continue
    return None

# --- Echauffement souris/clavier ---
async def human_mouse_warmup(page: Page):
    x = random.randint(80, 1400)
    y = random.randint(80, 700)
    await page.mouse.move(x, y, steps=random.randint(8, 20))
    await page.wait_for_timeout(human_delay_ms(700))
    # petit hover aléatoire
    for _ in range(random.randint(1, 3)):
        x += random.randint(-120, 120)
        y += random.randint(-80, 80)
        await page.mouse.move(max(0, x), max(0, y), steps=random.randint(6, 14))
        await page.wait_for_timeout(human_delay_ms(500))

# --- Scroll de lecture progressif ---
async def human_scroll_read(page: Page, min_steps: int = 3, max_steps: int = 6):
    steps = random.randint(min_steps, max_steps)
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(550, 1200))
        await page.wait_for_timeout(human_delay_ms(900))
    # micro remonter (très humain)
    if random.random() < 0.5:
        await page.mouse.wheel(0, -random.randint(200, 500))
        await page.wait_for_timeout(human_delay_ms(700))

# --- Navigation "propre" vers une URL ---
async def goto_clean(page: Page, url: str):
    await page.goto(url, wait_until="domcontentloaded")
    await settle(page)

async def human_typing(locator, text: str, delay_range: Tuple[int, int] = (70, 200)) -> None:
    """Tape `text` caractère par caractère avec micro-pauses aléatoires."""
    await locator.focus()
    await locator.fill("")
    for ch in text:
        await locator.type(ch, delay=random.randint(*delay_range))
        if ch == " " and random.random() < 0.25:
            await asyncio.sleep(random.uniform(0.08, 0.22))


async def slow_scroll(page: Page, steps: int | None = None, delay_range: Tuple[float, float] = (0.3, 1.0)) -> None:
    """Scroll progressif vers le bas pour charger les sections (simulateur â€œhumainâ€)."""
    if steps is None:
        steps = random.randint(2, 6)
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(400, 900))
        await asyncio.sleep(random.uniform(*delay_range))


async def simulate_browsing(page: Page) -> None:
    """Petits mouvements et scrolls pour mimer une navigation réelle."""
    await page.mouse.move(random.randint(100, 400), random.randint(100, 400))
    await asyncio.sleep(random.uniform(0.5, 1.2))
    for _ in range(random.randint(1, 3)):
        await page.mouse.wheel(0, random.randint(200, 600))
        await asyncio.sleep(random.uniform(0.4, 1.0))


async def visit_profile(page: Page, url: str, timeout_ms: int = settings.navigation_timeout_ms) -> None:
    """
    Ouvre un profil LinkedIn et attend un élément caractéristique du header.
    Relève PlaywrightTimeoutError si la page ne se stabilise pas.
    """
    await asyncio.sleep(random.uniform(1.2, 2.8))
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    # Header probable (multi-layout)
    header_sel = "main h1"
    max_retries = max(1, settings.max_retries)
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.locator(header_sel).first.wait_for(state="visible", timeout=timeout_ms)
            return
        except PlaywrightTimeoutError:
            logger.warning("Timeout profil (%s) tentative %d/%d", url, attempt, max_retries)
            if attempt == max_retries:
                raise
            await asyncio.sleep(min(2 ** attempt, 8))  # backoff simple plafonné

    await slow_scroll(page)
    await simulate_browsing(page)


async def click_section_button(page: Page, section_id: str) -> None:
    """
    Clique sur un bouton de section â€œVoir toutes les ...â€.
    `section_id` peut Ãªtre :
      - "navigation-index-see-all-education" / "navigation-index-see-all-experiences" (compat historique)
      - alias courts: "education" | "experience"
    Les sélecteurs viennent de config/selectors.json :
      linkedin.locales.<fr|en>.profile.buttons.see_all_education
      linkedin.locales.<fr|en>.profile.buttons.see_all_experience
    Fallback FR/EN par texte si non configuré.
    """
    loc = await _locale_hint(page)

    # 1) Normaliser en clé de config
    if section_id in (
        "education", "nav_education", "see_all_education", "navigation-index-see-all-education"
    ):
        key = "buttons.see_all_education"
    elif section_id in (
        "experience", "nav_experience", "see_all_experience", "navigation-index-see-all-experiences"
    ):
        key = "buttons.see_all_experience"
    else:
        # Si l’appelant passe directement une clé custom, on la laisse telle quelle
        key = section_id

    # 2) Essai avec sélecteurs externalisés
    selectors = sel_list("linkedin", "profile", key, locale=loc)

    # 3) Fallbacks raisonnables (texte FR/EN) si rien en config
    if not selectors:
        selectors = [
            "a:has-text('Tout afficher')",
            "button:has-text('Tout afficher')",
            "a:has-text('Voir plus')",
            "button:has-text('Voir plus')",
            "a:has-text('See all')",
            "button:has-text('See all')",
            "a:has-text('Show all')",
            "button:has-text('Show all')",
        ]

    btn = await _first_clickable(page, selectors, timeout=4000)
    if not btn:
        # Compat héritée : si on nous a donné un id précis, tenter le #id direct une dernière fois
        if section_id.startswith("navigation-index-see-all-"):
            try:
                loc2 = page.locator(f"#{section_id}").first
                if await loc2.count() > 0:
                    await loc2.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.7, 1.4))
                    await loc2.click()
                    await asyncio.sleep(random.uniform(1.0, 2.0))
            except Exception:
                pass
        return

    try:
        await btn.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.7, 1.4))
        await btn.click()
        # petite pause pour laisser charger la vue "voir plus"
        await asyncio.sleep(random.uniform(1.0, 2.0))
    except Exception:
        # On ne lève pas d’exception bloquante : le scrapper continuera en "without_click"
        return



