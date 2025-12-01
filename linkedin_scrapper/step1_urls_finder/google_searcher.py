# -*- coding: utf-8 -*-
"""
Recherche Google -> première URL de résultat (ciblage LinkedIn via la requête).

- Sauvegarde des cookies (Playwright storage_state) pour limiter les frictions.
- User-Agent persistant entre exécutions (stocké dans config/session_config.json).
- Pauses humaines + backoff simple pour réduire le risque de CAPTCHA.
- Sélecteurs externalisés dans config/selectors.json (google.search.input / results_links / next_page)

Entrée publique :
    async def main(query_mode: int, csv_file: str) -> pd.DataFrame
      Lit un CSV (colonnes attendues: 'nom', 'keywords'), lance les recherches,
      écrit un CSV de résultats incrémental daté dans data/outputs/, et renvoie
      un DataFrame en mémoire (nom, URL, keywords).

Exécution CLI :
    python -m link_searchs.google_searcher --mode 1 --csv path/to/input.csv
"""

from __future__ import annotations

from datetime import datetime
import asyncio
import random
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

import pandas as pd
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from linkedin_scrapper.config import settings
from linkedin_scrapper.utils_scripts.context_factory import launch_browser, new_context
from linkedin_scrapper.utils_scripts.selectors_registry import sel_list
from linkedin_scrapper.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = settings.navigation_timeout_ms

# --------------------------------------------------------------------------
# Dossiers projet (parent du dossier contenant CE fichier = racine projet)
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR   = PROJECT_ROOT / "config"
OUTPUTS_DIR  = PROJECT_ROOT / "data" / "outputs"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# Fichiers de persistance
COOKIES_FILE = CONFIG_DIR / "cookies_google.json"
SESSION_FILE = CONFIG_DIR / "session_config.json"

SAVE_TO_CSV = False

def _results_path(query_mode: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUTS_DIR / f"SEARCH{query_mode}_google_searchs_{stamp}.csv"

# Robustesse / tempos
MAX_RETRIES        = 3
RETRY_BASE_DELAY   = 3.0  # secondes
SHORT_SLEEP_RANGE  = (8, 15)    # sec entre requêtes
LONG_SLEEP_RANGE   = (60, 90)   # sec toutes les 10 requêtes

# User-Agents possibles (un seul sera "figé" par SESSION_FILE)
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

def _get_or_create_user_agent() -> str:
    """Retourne un UA stable entre exécutions (persisté dans SESSION_FILE)."""
    if SESSION_FILE.exists():
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            ua = data.get("user_agent")
            if isinstance(ua, str) and ua.strip():
                return ua
        except Exception:
            pass
    ua = random.choice(USER_AGENTS)
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump({"user_agent": ua}, f, ensure_ascii=False)
    return ua

# --- Helpers sélecteurs / interaction ---
async def _first_visible(page, selectors: list[str], timeout: int = DEFAULT_TIMEOUT):
    """
    Retourne le premier locator visible parmi une liste de sélecteurs CSS.
    Renvoie None si aucun ne matche.
    """
    for css in selectors:
        try:
            loc = page.locator(css).first
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None

async def _type_human_into_locator(page, locator, text: str) -> None:
    """Frappe caractère par caractère dans un Locator concret (focus + clear léger)."""
    try:
        await locator.focus()
    except Exception:
        pass
    try:
        await locator.fill("")
    except Exception:
        # si textarea readonly, on ignore : on tapera au clavier
        pass
    for ch in text:
        try:
            await locator.type(ch)
        except Exception:
            # fallback ultime: clavier global
            await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.05, 0.15))

def _canonicalize_google_result(href: Optional[str]) -> Optional[str]:
    """
    Convertit un lien Google /url?q=... en URL cible finale.
    Laisse les liens directs intacts.
    """
    if not href or not isinstance(href, str):
        return None
    try:
        from urllib.parse import urlparse, parse_qs, unquote
        u = urlparse(href)
        if ("google." in u.netloc) and (u.path == "/url"):
            q = parse_qs(u.query).get("q", [])
            if q:
                return unquote(q[0])
        return href
    except Exception:
        return href

# --------------------------------------------------------------------------
# Petites aides "humaines"
# --------------------------------------------------------------------------
async def _simulate_human_mouse(page) -> None:
    await page.mouse.move(random.randint(100, 400), random.randint(100, 400))
    await asyncio.sleep(random.uniform(1.5, 3.0))

# --------------------------------------------------------------------------
# Recherche principale
# --------------------------------------------------------------------------
async def search_google_name(
    name: str,
    user_agent: str,
    query_mode: int,
    keyword: str
) -> str | None:
    """
    Ouvre Google, tape la requête, retourne l'URL du premier résultat.

    query_mode:
        1 -> "site:linkedin.com/in {name} datascientest"
        2 -> "Linkedin {name} {keyword}"
    """
    pw, browser = await launch_browser(headless=False)
    context = None
    page = None
    try:
        storage_state_path = str(COOKIES_FILE) if COOKIES_FILE.exists() else None

        context = await new_context(
            browser,
            storage_state_path=storage_state_path,
            proxy=None,  # adapte si tu utilises un proxy
        )

        # (Optionnel) forcer l'UA choisi dans SESSION_FILE (en-tête HTTP)
        if isinstance(user_agent, str) and user_agent.strip():
            await context.set_extra_http_headers({"User-Agent": user_agent})

        page = await context.new_page()

        # 1) Google home
        await page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)

        # 2) Consentement cookies (si présent)
        try:
            for txt in ("Tout accepter", "J’accepte tout", "Accepter tout", "Accept all", "I agree"):
                btn = page.get_by_role("button", name=txt)
                if await btn.count() > 0:
                    await btn.first.click(timeout=3000)
                    break
        except Exception:
            pass

        # 3) Micro-mouvements "humains"
        await _simulate_human_mouse(page)

        # 4) Saisie requête (via sélecteurs externalisés)
        input_selectors = sel_list("google", "search", "input")
        box = await _first_visible(page, input_selectors, timeout=DEFAULT_TIMEOUT)
        if not box:
            logger.warning("Aucun sélecteur d'input Google n'a fonctionné: %s", input_selectors)
            return None

        await box.click()
        await asyncio.sleep(random.uniform(0.5, 1.2))

        if query_mode == 1:
            query = f"site:linkedin.com/in {name} datascientest"
        elif query_mode == 2:
            query = f"Linkedin {name} {keyword}"
        else:
            query = f"{name} linkedin"

        # frappe "humaine" dans le locator concret
        await _type_human_into_locator(page, box, query)
        await page.keyboard.press("Enter")

        # 5) Attente raisonnable du rendu
        await page.wait_for_load_state("domcontentloaded")
        try:
            # au moins un titre de résultat (externalisé)
            results_links_selectors = sel_list("google", "search", "results_links")
            if results_links_selectors:
                await page.locator(", ".join(results_links_selectors)).first.wait_for(
                    state="visible", timeout=DEFAULT_TIMEOUT
                )
            else:
                await page.wait_for_selector("h3", timeout=DEFAULT_TIMEOUT)
        except PlaywrightTimeoutError:
            pass

        # 6) Détection interstitiel/CAPTCHA (grossière)
        try:
            content = await page.content()
            captcha = (
                "sorry" in page.url.lower()
                or "captcha" in content.lower()
                or await page.locator('iframe[src*="recaptcha"]').count() > 0
                or await page.locator("form#captcha-form").count() > 0
            )
            if captcha:
                logger.warning("CAPTCHA détecté. Résous-le dans la fenêtre (pause).")
                try:
                    input("> Appuie sur Entrée une fois le captcha résolu...")
                except Exception:
                    pass
                await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass

        # 7) Premier résultat (via sélecteurs externalisés)
        link: Optional[str] = None
        try:
            sel_links = sel_list("google", "search", "results_links")
            if not sel_links:
                sel_links = ["a:has(h3)", "h3"]  # fallback minimaliste

            first_node = await _first_visible(page, sel_links, timeout=DEFAULT_TIMEOUT)
            if first_node:
                href = await first_node.evaluate(
                    "(el) => (el.closest && el.closest('a') && el.closest('a').href) || "
                    "(el.tagName === 'A' ? el.href : null)"
                )
                link = _canonicalize_google_result(href)
            else:
                # Fallback ultime: évaluer un <a:has(h3)>
                try:
                    href = await page.eval_on_selector("a:has(h3)", "el => el && el.href")
                    link = _canonicalize_google_result(href)
                except Exception:
                    link = None
        except Exception:
            link = None

        # 8) Persist cookies
        try:
            await context.storage_state(path=str(COOKIES_FILE))
        except Exception:
            pass

        await asyncio.sleep(random.uniform(4, 8))
        return link

    except Exception as e:
        logger.error("Erreur pour %s: %s", name, e, exc_info=True)
        return None

    finally:
        # Fermeture PROPRE
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


def _append_row_to_csv(path: Path, row: Dict[str, Any]) -> None:
    """Ajoute une ligne au CSV si SAVE_TO_CSV est True."""
    if not SAVE_TO_CSV:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    pd.DataFrame([row]).to_csv(path, mode="a", header=write_header, index=False, encoding="utf-8")

async def _sleep_human(i: int) -> None:
    if (i % 10) == 0:
        logger.info("Pause longue...")
        await asyncio.sleep(random.uniform(*LONG_SLEEP_RANGE))
    else:
        await asyncio.sleep(random.uniform(*SHORT_SLEEP_RANGE))

async def _retry_search(name: str, keyword: str, user_agent: str, query_mode: int) -> str:
    """
    Appelle search_google_name avec retries/backoff.
    Retourne '' en cas d'échec final.
    """
    attempt = 0
    while True:
        try:
            url = await search_google_name(name=name, user_agent=user_agent, query_mode=query_mode, keyword=keyword)
            return url or ""
        except Exception as e:
            attempt += 1
            if attempt >= MAX_RETRIES:
                logger.error("Echec pour '%s' après %d tentatives : %s", name, MAX_RETRIES, e, exc_info=True)
                return ""
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1.0)
            logger.warning("Tentative %d/%d pour '%s' → retry dans ~%.1fs", attempt, MAX_RETRIES, name, delay)
            await asyncio.sleep(delay)

# --------------------------------------------------------------------------
# Entrée publique
# --------------------------------------------------------------------------
async def main(query_mode: int, csv_file: str) -> pd.DataFrame:
    """
    Lit `csv_file` (colonnes: 'nom', 'keywords'), lance la recherche Google et
    renvoie un DataFrame en mémoire. Ecrit aussi un CSV incrémental daté.

    query_mode:
        1 -> site:linkedin.com/in {nom} datascientest
        2 -> Linkedin {nom} {keywords}
    """
    # Fichier de sortie daté
    results_file = _results_path(query_mode)

    # Charger l'entrée (string, vide -> "")
    df = pd.read_csv(csv_file, dtype="string").fillna("")
    if not {"nom", "keywords"}.issubset(df.columns):
        raise ValueError("Le CSV doit contenir les colonnes 'nom' et 'keywords'.")
    df["nom"] = df["nom"].astype("string").str.strip()
    df["keywords"] = df["keywords"].astype("string").str.strip()
    df = df[df["nom"] != ""].reset_index(drop=True)

    # Reprise éventuelle
    already = set()
    if results_file.exists():
        try:
            prev = pd.read_csv(results_file, dtype="string").fillna("")
            if "nom" in prev.columns:
                already = set(prev["nom"].astype("string").str.strip())
                logger.info("%d noms déjà traités · reprise activée.", len(already))
        except Exception as e:
            logger.warning("Impossible de relire %s pour reprise : %s", results_file, e, exc_info=True)

    user_agent = _get_or_create_user_agent()
    in_mem: List[Dict[str, str]] = []
    total = len(df)

    for i, row in enumerate(df.itertuples(index=False), start=1):
        name = getattr(row, "nom")
        kw   = getattr(row, "keywords")

        if name in already:
            continue

        logger.info("Recherche %d/%d : %s", i, total, name)
        url = await _retry_search(name=name, keyword=kw, user_agent=user_agent, query_mode=query_mode)
        rec = {"nom": name, "URL": url, "keywords": kw}
        in_mem.append(rec)

        try:
            _append_row_to_csv(results_file, rec)
        except Exception as e:
            logger.warning("Ecriture impossible dans %s pour '%s': %s (on continue)", results_file, name, e, exc_info=True)

        try:
            await _sleep_human(i)
        except Exception:
            break

    if SAVE_TO_CSV:
        logger.info("Terminé. Résultats : %s", results_file)
    else:
        logger.info("Terminé. Résultats gardés en mémoire (aucun CSV écrit).")

    return pd.DataFrame(in_mem, dtype="string").fillna("")



