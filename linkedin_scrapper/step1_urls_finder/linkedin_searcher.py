# -*- coding: utf-8 -*-
"""
LinkedIn search -> first profile URL (étape 1 - modes internes LinkedIn)

Ce module fournit des utilitaires pour lancer des recherches LinkedIn et
récupérer l’URL du **premier profil** retourné.

Fonctions clés
--------------
- pipeline_test(page, query) -> str|None
    Lance une recherche simple et renvoie la 1Ê³áµ‰ URL de profil.
- pipeline_batch_df(page, df_in, query_col='query') -> DataFrame
    Traite un DataFrame de requêtes (col. 'query') et renvoie (colonnes d’origine + query + url + status).
- entry_test(query) -> str|None
    Ouvre un navigateur + session, effectue une recherche unique, ferme.
- entry_batch(df_in, query_mode) -> DataFrame
    Ouvre un navigateur + session, construit les requêtes selon query_mode (1/3/4), exécute, ferme.
- entry_batch_modes_3_then_4(df_in, cooldown_seconds=(60,120)) -> (df_mode3, df_mode4)
    Ouvre **une seule session/page**, exécute **mode 3** puis **mode 4** avec une **pause longue**
    entre les deux, et renvoie les deux DataFrames de résultats. Idéal si vous voulez enchainer 3 puis 4
    avec un comportement plus "humain" (moins de reconnects soudains).

Notes
-----
- La session LinkedIn est gérée par `utils_scripts.linkedin_login.ensure_session` (réutilise le storage_state
  si présent, sinon propose un login manuel).
- Les entrées attendues pour le batch :
    * query_mode=1 : `query = nom`
    * query_mode=3 : `query = f"{nom} datascientest"`
    * query_mode=4 : `query = f"{nom} {keywords}"`  (requiert la colonne 'keywords')

Conseils anti-détection
-----------------------
- Des délais aléatoires sont intégrés (frappe "humaine", think-time, cooldown périodique).
- Pour enchainer deux modes sans rouvrir le navigateur, préférez `entry_batch_modes_3_then_4`
  qui ajoute un **cooldown (60-120 s par défaut)** entre 3 et 4.
"""

from __future__ import annotations
__all__ = [
    "entry_test",
    "entry_batch",
    "pipeline_test",
    "pipeline_batch_df",
    "entry_batch_modes_3_then_4",
]

import re
import random
import asyncio
import pandas as pd
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from contextlib import asynccontextmanager

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_scrapper.session_settings import STORAGE_STATE, USER_AGENT
from linkedin_scrapper.utils_scripts.linkedin_login import ensure_session
from linkedin_scrapper.config import settings
from linkedin_scrapper.utils_scripts.context_factory import launch_browser, new_context
from linkedin_scrapper.utils_scripts.guards import (
    HealthCounters, attach_response_watchers, AsyncRateLimiter,
    CircuitBreaker, guarded_action, backoff_sleep, is_captcha_or_checkpoint
)
from linkedin_scrapper.utils_scripts.selectors_registry import sel_list, pick_locale

from linkedin_scrapper.logging_setup import get_logger
logger = get_logger(__name__)

# =========================
# CONFIG
# =========================

HEADLESS = False

DEFAULT_TIMEOUT = settings.navigation_timeout_ms
HUMAN_MIN_DELAY = 40
HUMAN_MAX_DELAY = 110
MICRO_PAUSE_MS = 700

# --- Anti-détection : init des garde-fous pour cette page ---
def _init_guards(page: Page):
    """Attache les compteurs réseau à la page et prépare rate-limit + circuit-breaker."""
    health = HealthCounters()
    attach_response_watchers(page, health)
    limiter = AsyncRateLimiter(capacity=3, refill_per_sec=3/60.0)  # ≈ 1 action / 20s
    breaker = CircuitBreaker(open_after_score=3, sleep_when_open_s=1800)  # 30 min
    return health, limiter, breaker

# --- Locale helper (utilise navigator.language puis mappe via pick_locale) ---
async def _locale_hint(page: Page) -> str:
    try:
        nav_lang = await page.evaluate("() => navigator.language || navigator.userLanguage || 'en-US'")
    except Exception:
        nav_lang = "en-US"
    # pick_locale renvoie "fr" ou "en" selon selectors.json
    return pick_locale("linkedin", nav_lang)

# =========================
# HELPERS
# =========================
async def human_typing(
    locator,
    text: str,
    min_delay: int = HUMAN_MIN_DELAY,
    max_delay: int = HUMAN_MAX_DELAY,
) -> None:
    """
    Tape `text` caractère par caractère avec des pauses aléatoires.
    - `min_delay`/`max_delay` en millisecondes (par caractère)
    - micro-pauses sur les espaces pour mimer la réflexion
    """
    await locator.focus()
    await locator.fill("")
    for ch in text:
        await locator.type(ch, delay=random.randint(min_delay, max_delay))
        if ch == " " and random.random() < 0.25:
            await asyncio.sleep(random.uniform(0.08, 0.22))
    # mini pause finale
    await asyncio.sleep(random.uniform(0.12, 0.25))


def canonicalize_linkedin_url(href: str) -> str:
    """Retourne une URL canonique (netloc en minuscule, sans query/fragment, slash final)."""
    parts = urlsplit(href)
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}/"


# =========================
# WAITERS ciblés (éviter networkidle sur LinkedIn)
# =========================
async def wait_feed_ready(page: Page, timeout: int = DEFAULT_TIMEOUT) -> None:
    """
    Attendre que la page /feed soit utilisable en s'appuyant sur les sélecteurs externalisés
    (barre de recherche LinkedIn), avec détection auto FR/EN.
    """
    await page.wait_for_load_state("domcontentloaded")
    loc = await _locale_hint(page)
    selectors = sel_list("linkedin", "search", "input", locale=loc)
    last_err = None
    for css in selectors:
        try:
            await page.locator(css).first.wait_for(state="visible", timeout=timeout)
            return
        except Exception as e:
            last_err = e
            continue
    raise PlaywrightTimeoutError(f"Aucun sélecteur 'linkedin.search.input' visible pour locale '{loc}' → {selectors}") from last_err

# =========================
# SEARCH & EXTRACT
# =========================
async def submit_search(page: Page, query: str) -> bool:
    """
    Tape `query` dans la barre de recherche et valide.
    S'appuie sur config/selectors.json -> linkedin.locales.<fr|en>.search.input
    (locale détectée automatiquement via navigator.language).
    """
    try:
        loc = await _locale_hint(page)
        selectors = sel_list("linkedin", "search", "input", locale=loc)

        # Trouver la barre de recherche via la liste externalisée
        box = None
        for css in selectors:
            cand = page.locator(css).first
            try:
                await cand.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
                box = cand
                break
            except Exception:
                continue

        if not box:
            logger.warning("Aucun sélecteur d'input LinkedIn n'a fonctionné (locale=%s): %s", loc, selectors)
            return False

        await box.click()

        # frappe "humaine"
        await human_typing(box, query, min_delay=HUMAN_MIN_DELAY, max_delay=HUMAN_MAX_DELAY)

        # Valider (double Enter = fallback si la 1Ê³áµ‰ touche ne part pas)
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_url(re.compile(r"/search/results/"), timeout=DEFAULT_TIMEOUT)
        except PlaywrightTimeoutError:
            await page.keyboard.press("Enter")
            await page.wait_for_url(re.compile(r"/search/results/"), timeout=DEFAULT_TIMEOUT)

        return True

    except PlaywrightTimeoutError:
        logger.warning("Barre de recherche ou résultats introuvables (timeout).")
    except Exception as e:
        logger.error("Erreur recherche '%s': %s", query, e, exc_info=True)
    return False


async def get_first_profile_url(page: Page) -> str | None:
    """
    Retourne l'URL canonique du premier profil affiché, sinon None.
    S'appuie sur config/selectors.json -> linkedin.locales.<fr|en>.search.result_profile_link
    (locale détectée automatiquement).
    """
    try:
        await page.wait_for_url(re.compile(r"/search/results/"), timeout=DEFAULT_TIMEOUT)

        loc = await _locale_hint(page)
        selectors = sel_list("linkedin", "search", "result_profile_link", locale=loc)
        selector = ", ".join(selectors) if selectors else "a[href^='https://www.linkedin.com/in/']"  # ultime fallback

        first_link = page.locator(selector).first
        await first_link.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)

        href = await first_link.get_attribute("href")
        if not href:
            return None
        return canonicalize_linkedin_url(href)

    except PlaywrightTimeoutError:
        logger.warning("Aucun lien de profil visible (timeout).")
    except Exception as e:
        logger.error("Extraction URL premier profil: %s", e, exc_info=True)
    return None


# =========================
# PIPELINES (test / batch)
# =========================
async def pipeline_test(page: Page, query: str) -> str | None:
    """Recherche un seul nom et retourne l'URL du premier profil (ou None), avec garde-fous."""
    health, limiter, breaker = _init_guards(page)

    # breaker déjà ouvert → on ne tente pas
    if breaker.is_open:
        logger.warning("[BREAKER] Pause longue active. Abandon pipeline_test.")
        return None

    try:
        # Saisie + submit sous rate-limit
        async with guarded_action(page, health, limiter):
            ok = await submit_search(page, query)
        if not ok:
            return None

        # Petit settle puis controle captcha/checkpoint
        await page.wait_for_timeout(random.randint(300, 800))
        if await is_captcha_or_checkpoint(page):
            logger.warning("[WARN] Challenge après submit_search → backoff")
            await backoff_sleep(base=10, factor=2.0, attempt=max(1, health.score()), cap=900)
            if breaker.maybe_open(health):
                logger.error("[BREAKER] Score élevé (%d).", health.score())
                return None
            # reset doux
            try:
                async with guarded_action(page, health, limiter):
                    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            except Exception:
                pass
            return None

        # Extraction 1er profil sous rate-limit
        async with guarded_action(page, health, limiter):
            url = await get_first_profile_url(page)
        return url

    except Exception as e:
        logger.error("pipeline_test error: %s", e, exc_info=True)
        return None



async def pipeline_batch_df(page: Page, df_in: pd.DataFrame, query_col: str = "query") -> pd.DataFrame:
    """
    Exécute une recherche LinkedIn pour chaque ligne de `df_in` et retourne
    un DataFrame contenant les colonnes d'origine + ['query', 'URL', 'status'].
    Intègre rate-limit, détection captcha/checkpoint, backoff et breaker.
    """
    health, limiter, breaker = _init_guards(page)

    # 1) Choix de la colonne de requête
    if query_col not in df_in.columns:
        if "name" in df_in.columns:
            query_col = "name"
        else:
            raise ValueError("Le DataFrame doit contenir une colonne 'query' ou 'name'.")

    # 2) Copie + id technique stable
    df = df_in.copy()
    had_row_id = "row_id" in df.columns
    if not had_row_id:
        df["row_id"] = range(len(df))

    # 3) Normaliser/peupler la colonne 'query'
    df["query"] = (
        df[query_col]
        .astype(str)
        .map(lambda x: x.strip())
        .replace({"": None, "nan": None, "None": None})
    )

    results = []
    total = len(df)

    # Pacing
    THINK_MIN, THINK_MAX = 1000, 2000
    JITTER_MIN, JITTER_MAX = 200, 800
    LONG_COOLDOWN_EVERY = 8
    LONG_COOLDOWN_MIN, LONG_COOLDOWN_MAX = 8000, 15000
    BACKOFF_MIN, BACKOFF_MAX = 3000, 6000
    SETTLE_MIN, SETTLE_MAX = 300, 800

    for i, row in enumerate(df.itertuples(index=False), start=1):
        # breaker ouvert → on arrête proprement la boucle (tu peux switcher de compte/proxy ici)
        if breaker.is_open:
            logger.warning("[BREAKER] Pause longue active. Arrêt du batch à i=%d.", i)
            break

        q = row.query
        row_id = row.row_id

        # Think-time humain
        await page.wait_for_timeout(random.randint(THINK_MIN, THINK_MAX))
        logger.info("[%d/%d] %s", i, total, q)

        try:
            if q is None:
                results.append({"row_id": row_id, "URL": "", "status": "empty_query"})
            else:
                # Submit sous rate-limit
                async with guarded_action(page, health, limiter):
                    ok = await submit_search(page, q)

                if not ok:
                    results.append({"row_id": row_id, "URL": "", "status": "search_failed"})
                    await page.wait_for_timeout(random.randint(BACKOFF_MIN, BACKOFF_MAX))
                else:
                    # settle + détection challenge
                    await page.wait_for_timeout(random.randint(SETTLE_MIN, SETTLE_MAX))
                    if await is_captcha_or_checkpoint(page):
                        results.append({"row_id": row_id, "URL": "", "status": "challenge"})
                        logger.warning("[WARN] Challenge détecté à i=%d → backoff", i)
                        await backoff_sleep(base=10, factor=2.0, attempt=max(1, health.score()), cap=900)

                        # reset doux : retour /feed
                        try:
                            async with guarded_action(page, health, limiter):
                                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                        except Exception:
                            pass

                        # ouvrir breaker si nécessaire
                        if breaker.maybe_open(health):
                            logger.error("[BREAKER] Score élevé (%d). Mise en pause longue.", health.score())
                            break
                    else:
                        # Extraction sous rate-limit
                        async with guarded_action(page, health, limiter):
                            url = await get_first_profile_url(page)
                        results.append({
                            "row_id": row_id,
                            "URL": url or "",
                            "status": ("ok" if url else "no_result"),
                        })

        except Exception as e:
            results.append({"row_id": row_id, "URL": "", "status": f"error:{e}"})
            await page.wait_for_timeout(random.randint(BACKOFF_MIN, BACKOFF_MAX))

        # Micro-pause + jitter entre requêtes
        await page.wait_for_timeout(MICRO_PAUSE_MS + random.randint(JITTER_MIN, JITTER_MAX))

        # Cooldown périodique
        if i % LONG_COOLDOWN_EVERY == 0 and i < total:
            await page.wait_for_timeout(random.randint(LONG_COOLDOWN_MIN, LONG_COOLDOWN_MAX))

    # Fusion finale
    df_res = pd.DataFrame(results)
    df_out = df.merge(df_res, on="row_id", how="left")

    # Nettoyage du row_id si créé ici
    if not had_row_id and "row_id" in df_out.columns:
        df_out = df_out.drop(columns=["row_id"])

    base_cols = [c for c in df_in.columns if c != "row_id"]
    tail_cols = [c for c in ["query", "URL", "status"] if c not in base_cols]
    return df_out[base_cols + tail_cols]



# =========================
# ENTREES ASYNC PAR MODE (avec ouverture/fermeture navigateur)
# =========================

async def entry_test(query: str) -> str | None:
    """
    Crée le navigateur, garantit une session (login manuel si besoin),
    exécute pipeline_test, ferme et retourne l'URL.
    """
    pw, browser = await launch_browser(headless=HEADLESS)
    context = None
    page = None
    try:
        storage = STORAGE_STATE if Path(STORAGE_STATE).exists() else None
        context = await new_context(
            browser,
            storage_state_path=storage,
            proxy=None,  # adapte si tu utilises un proxy
        )
        context.set_default_timeout(DEFAULT_TIMEOUT)
        context.set_default_navigation_timeout(DEFAULT_TIMEOUT)

        page = await context.new_page()

        #  Session LinkedIn
        logged, page = await ensure_session(
            page,
            storage_state_path=STORAGE_STATE,
            user_agent=USER_AGENT,
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        if not logged:
            return None

        try:
            await wait_feed_ready(page, timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass

        url = await pipeline_test(page, query)
        return url

    finally:
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

async def entry_batch(
    df_in: pd.DataFrame,
    query_mode: Literal[1, 3, 4]
) -> pd.DataFrame:
    """
    Crée le navigateur, garantit une session (login manuel si besoin),
    exécute pipeline_batch_df et retourne (query, url, status).
    """
    pw, browser = await launch_browser(headless=HEADLESS)
    context = None
    page = None
    try:
        storage = STORAGE_STATE if Path(STORAGE_STATE).exists() else None
        logger.info("[session] STORAGE_STATE: %s → %s", STORAGE_STATE, "present" if storage else "absent")

        context = await new_context(
            browser,
            storage_state_path=storage,
            proxy=None,  # adapte si besoin
        )
        context.set_default_timeout(DEFAULT_TIMEOUT)
        context.set_default_navigation_timeout(DEFAULT_TIMEOUT)

        page = await context.new_page()

        # Session LinkedIn
        logged, page = await ensure_session(
            page,
            storage_state_path=STORAGE_STATE,
            user_agent=USER_AGENT,
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        if not logged:
            return pd.DataFrame(columns=["query", "url", "status"])

        try:
            await wait_feed_ready(page, timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass

        # ---- Prépare les requêtes ----
        df = df_in.copy()
        if "nom" not in df.columns:
            raise ValueError("Colonne 'nom' manquante dans df_in.")
        df["nom"] = df["nom"].astype(str).fillna("").str.strip()
        
        if query_mode == 3:
            df["query"] = df["nom"].apply(lambda n: f"{n} datascientest".strip())
        elif query_mode == 4:
            if "keywords" not in df.columns:
                raise ValueError("Colonne 'keywords' requise pour query_mode=4.")
            df["keywords"] = df["keywords"].astype(str).fillna("").str.strip()
            df["query"] = (df["nom"] + " " + df["keywords"].where(df["keywords"] != "", "")).str.strip()
        else:
            raise ValueError("query_mode invalide (attendu 1, 3 ou 4).")

        df["query"] = df["query"].str.split().str.join(" ")

        # ---- Batch (avec garde-fous internes) ----
        df_out = await pipeline_batch_df(page, df, query_col="query")
        return df_out

    finally:
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


# =========================
# NOUVEAU : enchainer 3 puis 4 dans UNE SEULE session/page
# =========================
@asynccontextmanager
async def _open_linkedin_session(headless: bool = HEADLESS):
    """
    Ouvre un navigateur Chromium via notre factory, crée un context configuré,
    garantit la session via ensure_session, et renvoie (page, health, limiter, breaker).
    Ferme proprement en sortie.
    """
    pw, browser = await launch_browser(headless=headless)
    context = None
    page = None
    try:
        storage = STORAGE_STATE if Path(STORAGE_STATE).exists() else None
        context = await new_context(
            browser,
            storage_state_path=storage,
            proxy=None,  # adapte si besoin
        )
        context.set_default_timeout(DEFAULT_TIMEOUT)
        context.set_default_navigation_timeout(DEFAULT_TIMEOUT)

        page = await context.new_page()

        # Session LinkedIn (réutilisation ou login manuel)
        logged, page = await ensure_session(
            page,
            storage_state_path=STORAGE_STATE,
            user_agent=USER_AGENT,
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        if not logged:
            raise RuntimeError("Session LinkedIn indisponible (login manuel non effectué ou échec).")

        try:
            await wait_feed_ready(page, timeout=DEFAULT_TIMEOUT)
        except Exception:
            pass

        # Garde-fous pour cette page
        health, limiter, breaker = _init_guards(page)

        yield page, health, limiter, breaker

    finally:
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


async def _run_batch_on_page(
    page: Page,
    df_in: pd.DataFrame,
    query_mode: Literal[1, 3, 4],
    health: HealthCounters,
    limiter: AsyncRateLimiter,
    breaker: CircuitBreaker,
) -> pd.DataFrame:
    """
    Lance pipeline_batch_df SUR UNE PAGE DEJA OUVERTE/CONNECTEE.
    Les garde-fous (health/limiter/breaker) sont déjà initialisés pour cette page.
    """
    df = df_in.copy()
    if "nom" not in df.columns:
        raise ValueError("Colonne 'nom' manquante dans df_in.")
    df["nom"] = df["nom"].astype(str).fillna("").str.strip()

    if query_mode == 1:
        df["query"] = df["nom"]
    elif query_mode == 3:
        df["query"] = df["nom"].apply(lambda n: f"{n} datascientest".strip())
    elif query_mode == 4:
        if "keywords" not in df.columns:
            raise ValueError("Colonne 'keywords' requise pour query_mode=4.")
        df["keywords"] = df["keywords"].astype(str).fillna("").str.strip()
        df["query"] = (df["nom"] + " " + df["keywords"].where(df["keywords"] != "", "")).str.strip()
    else:
        raise ValueError("query_mode invalide (attendu 1, 3 ou 4).")

    df["query"] = df["query"].str.split().str.join(" ")

    # pipeline_batch_df ré-initialise normalement ses garde-fous ; ici on veut réutiliser les mêmes.
    # Pour rester simple et éviter de dupliquer la logique, on ré-appelle pipeline_batch_df tel quel :
    return await pipeline_batch_df(page, df, query_col="query")


async def entry_batch_modes_3_then_4(
    df_in: pd.DataFrame,
    cooldown_seconds: tuple[int, int] = (60, 120),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Exécute **mode 3** puis **mode 4** dans **la même session**.
    Pause longue aléatoire entre les deux pour rester "humain".
    Retourne (df_mode3, df_mode4).
    """
    async with _open_linkedin_session(headless=HEADLESS) as (page, health, limiter, breaker):
        df_mode3 = await _run_batch_on_page(page, df_in, query_mode=3, health=health, limiter=limiter, breaker=breaker)

        pause = random.uniform(*cooldown_seconds)
        logger.info("Cooldown entre mode 3 et 4 ~%.1fs", pause)
        await asyncio.sleep(pause)

        df_mode4 = await _run_batch_on_page(page, df_in, query_mode=4, health=health, limiter=limiter, breaker=breaker)
        return df_mode3, df_mode4



