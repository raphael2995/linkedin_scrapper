# step2_profile_scraper/scrape_profiles.py
# -*- coding: utf-8 -*-

"""
Scrape des profils LinkedIn (Étape 2) à partir d'un CSV d'URLs produit à l'étape 1.

Entrée (CSV) :
    - colonnes attendues : URL (peut être vide) ; nom, keywords, status, score (optionnelles)
    - ⚠️ AUCUN FILTRE : on garde toutes les lignes (même sans URL)

Sorties :
    - JSONL incrémental (une ligne JSON par profil) dans data/outputs/step2_profiles/
    - Export CSV miroir auto à la fin (même base de nom)
    - Ajout d'un champ `url_quality` :
        * "URL sûre"        : URL http(s) ET (status == "ok" OU score >= 0.90)
        * "URL douteuse"    : URL http(s) ET (status == "fallback_best" OU 0.70 <= score < 0.90)
        * "URL faible"      : URL http(s) mais score/status trop faibles
        * "Aucune URL"      : URL absente/non valide

Usage (depuis la racine du projet) :
    python -m step2_profile_scraper.scrape_profiles \
        --csv data/outputs/pipeline_search_results_YYYYMMDD_HHMM.csv \
        --tag client_fr \
        --limit 50 \
        --save-every 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from pathlib import Path
from typing import Dict, Any, List, Optional

import pandas as pd
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from linkedin_scrapper.config import settings

# Chemins & session centralisés
from linkedin_scrapper.session_settings import STORAGE_STATE, USER_AGENT, PROFILES_DIR, timestamp_min
from linkedin_scrapper.utils_scripts.linkedin_login import ensure_session
from linkedin_scrapper.utils_scripts.utils_async import run_coro
from linkedin_scrapper.utils_scripts.context_factory import launch_browser, new_context

# Helpers step2
from linkedin_scrapper.step2_profile_scraper.web_interactions import (
    visit_profile,
    click_section_button,
    human_mouse_warmup,
    human_scroll_read,
    settle,
    goto_clean,
)
from linkedin_scrapper.step2_profile_scraper.linkedin_sections_extractor import (
    extract_formation,
    extract_experiences_with_click,
    extract_experiences_without_click,
)
from linkedin_scrapper.utils_scripts.guards import (
    HealthCounters, attach_response_watchers, AsyncRateLimiter,
    CircuitBreaker, guarded_action, backoff_sleep, is_captcha_or_checkpoint
)

from dataclasses import dataclass, asdict
from datetime import datetime

from linkedin_scrapper.logging_setup import get_logger
logger = get_logger(__name__)

LOGS_DIR = PROFILES_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class RunStats:
    run_id: str
    started_at: str
    finished_at: str | None = None
    total_rows: int = 0
    processed: int = 0
    ok: int = 0
    skip_no_url: int = 0
    challenge: int = 0
    timeout: int = 0
    error: int = 0
    no_result: int = 0

    def bump(self, status: str):
        self.processed += 1
        s = (status or "").lower()
        if s == "ok":
            self.ok += 1
        elif s == "skip_no_url":
            self.skip_no_url += 1
        elif s.startswith("challenge"):
            self.challenge += 1
        elif s == "timeout":
            self.timeout += 1
        elif s.startswith("error"):
            self.error += 1
        elif s == "no_result":
            self.no_result += 1

def _append_events(path: Path, events: List[Dict[str, Any]]) -> None:
    """Append JSONL d'événements minimalistes (1 par profil)."""
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

# ---- Configs de scraping
HEADLESS = os.getenv("HEADLESS", "false").lower() in {"1", "true", "yes"}
DEFAULT_TIMEOUT = settings.navigation_timeout_ms
# pacing plus humain pour réduire les flags
PAUSE_BETWEEN_PROFILES = (6.0, 12.0)   # secondes
LONG_PAUSE_EVERY = 10
LONG_PAUSE_RANGE = (180, 300)          # secondes

# ---- Anti-détection (tuning)
RATE_LIMIT_CAPACITY = 3                # nb d'actions "réseau" dans le burst
RATE_LIMIT_REFILL_PER_SEC = 3 / 60.0   # ~3 actions par minute ≈ 1 toutes 20s
BREAKER_SCORE_THRESHOLD = 6            # seuil plus haut avant d'ouvrir
BREAKER_SLEEP_SECONDS = 900            # 15 min

def _stamp() -> str:
    # horodatage à la minute (YYYYMMDD_HHMM), aligné avec session_settings
    return timestamp_min()

def _default_out(tag: Optional[str]) -> Path:
    # sorties propres dans data/outputs/step2_profiles/
    base = f"profiles_{(tag + '_') if tag else ''}{_stamp()}.jsonl"
    return PROFILES_DIR / base

def _append_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    """Append JSONL (crée le fichier et les dossiers si nécessaires)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

def _safe_score(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("score", 0) or 0)
    except Exception:
        return 0.0

def _classify_url_quality(row: Dict[str, Any]) -> str:
    """
    Retourne une étiquette lisible de la qualité de l’URL d’entrée, en se basant
    sur `status` + `score` (step1) + validité http(s).
    """
    url = row.get("URL") or row.get("url") or ""
    status = (row.get("status") or "").strip().lower()
    score = _safe_score(row)

    if not isinstance(url, str) or not url.startswith("http"):
        return "Aucune URL"

    if status == "ok" or score >= 0.90:
        return "URL sûre"
    if status == "fallback_best" or 0.70 <= score < 0.90:
        return "URL douteuse"
    return "URL faible"

def _prepare_input_df(csv_path: Path, limit: Optional[int]) -> pd.DataFrame:
    """
    Charge l’entrée SANS FILTRE (on garde tous les noms ; URL peut être vide).
    """
    df = pd.read_csv(csv_path, dtype="string").fillna("")
    if limit:
        df = df.head(limit)
    return df.reset_index(drop=True)

async def _extract_one(
    page,
    row: Dict[str, Any],
    health: Optional[HealthCounters] = None,
    limiter: Optional[AsyncRateLimiter] = None,
    breaker: Optional[CircuitBreaker] = None,
) -> Dict[str, Any]:
    """
    Ouvre un profil LinkedIn (si URL valide), simule des actions humaines,
    clique sur Éducation/Expériences si possible, puis extrait les données.
    Protège les navigations avec rate-limit + détection captcha/checkpoint.

    Retourne : {nom, URL, url_quality, formations, experiences, status}
    status ∈ { ok | skip_no_url | skip_breaker_open | challenge:* | timeout | error:* }
    """
    name = row.get("nom") or row.get("Nom") or ""
    url = row.get("URL") or row.get("url") or ""
    url_quality = _classify_url_quality(row)

    if not isinstance(url, str) or not url.startswith("http"):
        return {
            "nom": name,
            "URL": url,
            "url_quality": url_quality,
            "formations": [],
            "experiences": [],
            "status": "skip_no_url",
        }

    # Si le circuit breaker est ouvert, on ne tente pas
    if breaker and breaker.is_open:
        return {
            "nom": name,
            "URL": url,
            "url_quality": url_quality,
            "formations": [],
            "experiences": [],
            "status": "skip_breaker_open",
        }

    try:
        # --- Aller sur le profil (limité + check captcha après)
        if health and limiter:
            async with guarded_action(page, health, limiter):
                await visit_profile(page, url)
        else:
            await visit_profile(page, url)

        await settle(page)              # petite stabilisation DOM
        await human_mouse_warmup(page)  # mouvements souris
        await human_scroll_read(page)   # scroll “lecture”
        await settle(page)

        # --- Détection challenge/captcha éventuel
        if await is_captcha_or_checkpoint(page):
            return {
                "nom": name,
                "URL": url,
                "url_quality": url_quality,
                "formations": [],
                "experiences": [],
                "status": "challenge:captcha_or_checkpoint",
            }

        await asyncio.sleep(random.uniform(*PAUSE_BETWEEN_PROFILES))

        # --- ÉDUCATION
        try:
            # ⚠️ clé alignée avec selectors.json
            await click_section_button(page, "see_all_education")
            await settle(page)
        except Exception:
            pass
        formations = await extract_formation(page)

        # --- Retour page principale du profil avant Expériences
        try:
            went_back = False
            try:
                await page.go_back()
                went_back = True
            except Exception:
                pass

            if (not went_back) or (page.url != url):
                if health and limiter:
                    async with guarded_action(page, health, limiter):
                        await goto_clean(page, url)  # navigation propre + settle()
                else:
                    await goto_clean(page, url)

            # “lecture” légère avant extractions suivantes
            await human_scroll_read(page, min_steps=2, max_steps=4)
            await settle(page)

        except Exception:
            # force le retour propre si le back rate
            if health and limiter:
                async with guarded_action(page, health, limiter):
                    await goto_clean(page, url)
            else:
                await goto_clean(page, url)

        # --- EXPÉRIENCES
        try:
            # ⚠️ clé alignée avec selectors.json
            await click_section_button(page, "see_all_experience")
            await settle(page)
            experiences = await extract_experiences_with_click(page)
        except Exception:
            experiences = await extract_experiences_without_click(page)

        return {
            "nom": name,
            "URL": url,
            "url_quality": url_quality,
            "formations": formations or [],
            "experiences": experiences or [],
            "status": "ok",
        }

    except PlaywrightTimeoutError:
        return {
            "nom": name,
            "URL": url,
            "url_quality": url_quality,
            "formations": [],
            "experiences": [],
            "status": "timeout",
        }
    except Exception as e:
        return {
            "nom": name,
            "URL": url,
            "url_quality": url_quality,
            "formations": [],
            "experiences": [],
            "status": f"error:{e}",
        }

async def main(
    input_csv: Path,
    out_jsonl: Optional[Path],
    tag: Optional[str],
    limit: Optional[int],
    save_every: int
) -> Path:
    """
    1) Ouvre le navigateur, garantit la session via ensure_session (login manuel si besoin).
    2) Charge l’entrée (sans filtre) et itère sur chaque ligne.
    3) Sauvegarde incrémentale en JSONL toutes les `save_every` lignes.
    4) Exporte aussi un CSV miroir à la fin (incluant `url_quality`).
    """
    out_jsonl = out_jsonl or _default_out(tag)
    df = _prepare_input_df(input_csv, limit)
    total = len(df)
    logger.info("Entrées à traiter : %d — sortie : %s", total, out_jsonl)

    # --- Observabilité ultra-light ---
    run_id = _stamp()  # même horodatage que tes sorties
    events_file = LOGS_DIR / f"events_{run_id}.jsonl"

    stats = RunStats(run_id=run_id, started_at=datetime.utcnow().isoformat() + "Z")
    stats.total_rows = total
    event_buffer: List[Dict[str, Any]] = []
    EVENT_FLUSH_EVERY = max(5, save_every)

    # Reprise : ne pas re-scraper les URLs déjà présentes
    already_urls: set[str] = set()
    if out_jsonl.exists():
        try:
            prev = pd.read_json(out_jsonl, lines=True)
            if "URL" in prev.columns:
                already_urls = set(prev["URL"].dropna().astype(str).tolist())
        except Exception:
            pass

    buffer: List[Dict[str, Any]] = []

    # ---- OUVERTURE VIA FACTORY (canal Chrome + empreinte + stealth) ----
    pw, browser = await launch_browser(headless=HEADLESS)
    context = None
    page = None
    try:
        storage = STORAGE_STATE if Path(STORAGE_STATE).exists() else None
        context = await new_context(
            browser,
            storage_state_path=storage,
            proxy=None,  # ⇐ passe ton proxy ici si besoin
        )
        page = await context.new_page()

        # ✅ Session LinkedIn (réutilisation ou login manuel)
        logged, page = await ensure_session(
            page,
            storage_state_path=STORAGE_STATE,
            user_agent=USER_AGENT,
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        if not logged:
            raise RuntimeError("Session LinkedIn indisponible (login manuel non effectué ou échec).")

        # 🔥 WARMUP : navigation safe sur le feed pour “imprégner” le contexte
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await settle(page)
        await human_mouse_warmup(page)
        await human_scroll_read(page, min_steps=3, max_steps=6)
        await settle(page)

        # --- Garde-fous anti-détection (compteurs, rate-limit, breaker) ---
        health = HealthCounters()
        attach_response_watchers(page, health)

        limiter = AsyncRateLimiter(
            capacity=RATE_LIMIT_CAPACITY,
            refill_per_sec=RATE_LIMIT_REFILL_PER_SEC
        )

        breaker = CircuitBreaker(
            open_after_score=BREAKER_SCORE_THRESHOLD,
            sleep_when_open_s=BREAKER_SLEEP_SECONDS
        )

        for i, row in enumerate(df.to_dict(orient="records"), start=1):
            url = row.get("URL") or row.get("url") or ""

            # Reprise : si on a déjà scrapé EXACTEMENT cette URL, on l’ignore
            if isinstance(url, str) and url.startswith("http") and url in already_urls:
                logger.info("[%d/%d] déjà présent → skip", i, total)
                continue

            # Si breaker ouvert -> on arrête (ou on pourrait switch de compte/proxy ici)
            if breaker.is_open:
                logger.warning("[BREAKER] Compte en pause longue. Arrêt de la boucle (ou switch requis).")
                break

            logger.info("[%d/%d] %s", i, total, (url or "— sans URL —"))
            data = await _extract_one(page, row, health=health, limiter=limiter, breaker=breaker)
            buffer.append(data)

            # --- Event ligne + stats ---
            event_buffer.append({
                "run_id": run_id,
                "i": i,
                "total": total,
                "ts": datetime.utcnow().isoformat() + "Z",
                "input": {
                    "nom": row.get("nom") or row.get("Nom") or "",
                    "URL_in": url or "",
                    "url_quality": _classify_url_quality(row),
                },
                "output": {
                    "status": data.get("status"),
                    "formations_count": len(data.get("formations") or []),
                    "experiences_count": len(data.get("experiences") or []),
                    "URL": data.get("URL") or ""
                }
            })
            stats.bump(data.get("status", ""))

            # Gestion des cas "challenge"
            status = (data.get("status") or "").lower()
            if status.startswith("challenge:"):
                logger.warning("[WARN] Challenge détecté (%s) → backoff + tentative de reset", status)

                # backoff exponentiel proportionnel au score courant
                attempt = max(1, health.score())
                await backoff_sleep(base=10, factor=2.0, attempt=attempt, cap=900)

                # tentative de "reset doux" : retour au feed (protégé par rate-limit)
                try:
                    async with guarded_action(page, health, limiter):
                        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                        await settle(page)
                except Exception:
                    pass

                # ouvre le breaker si le score est trop élevé
                if breaker.maybe_open(health):
                    logger.error("[BREAKER] Score élevé (%d). Mise en pause longue pour ce compte.", health.score())
                    break

            # Sauvegarde incrémentale
            if (i % save_every) == 0:
                _append_jsonl(out_jsonl, buffer)
                for r in buffer:
                    u = r.get("URL", "")
                    if isinstance(u, str) and u.startswith("http"):
                        already_urls.add(u)
                buffer.clear()

                # flush events
                if len(event_buffer) >= EVENT_FLUSH_EVERY:
                    _append_events(events_file, event_buffer)
                    event_buffer.clear()

                if (i % LONG_PAUSE_EVERY) == 0:
                    pause = random.uniform(*LONG_PAUSE_RANGE)
                    logger.info("Pause longue ~%ds", int(pause))
                    await asyncio.sleep(pause)

            # reset doux entre PROFILS pour réduire les flags
            try:
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                await settle(page)
            except Exception:
                pass

        # Flush final
        if buffer:
            _append_jsonl(out_jsonl, buffer)
            buffer.clear()

        # Flush final events
        if event_buffer:
            _append_events(events_file, event_buffer)
            event_buffer.clear()

        # Export CSV auto (miroir du JSONL) — encodage UTF-8-SIG pour Excel
        try:
            if out_jsonl.exists() and out_jsonl.stat().st_size > 0:
                df_out = pd.read_json(out_jsonl, lines=True)
                csv_out = out_jsonl.with_suffix(".csv")
                df_out.to_csv(csv_out, index=False, encoding="utf-8-sig")
                logger.info("Export CSV : %s", csv_out)
            else:
                logger.warning("Fichier JSONL vide, export CSV sauté.")
        except Exception as e:
            logger.error("Export CSV impossible : %s", e, exc_info=True)

        # Résumé du run
        stats.finished_at = datetime.utcnow().isoformat() + "Z"
        try:
            with (LOGS_DIR / f"summary_{run_id}.json").open("w", encoding="utf-8") as f:
                json.dump(asdict(stats), f, ensure_ascii=False, indent=2)
            logger.info("Résumé écrit: %s", LOGS_DIR / f"summary_{run_id}.json")
            logger.info("Events écrits: %s", events_file)
        except Exception as e:
            logger.warning("Impossible d'écrire le résumé du run: %s", e, exc_info=True)

        logger.info("Terminé : %s", out_jsonl)
        return out_jsonl

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


# =======================
# CLI
# =======================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scraper LinkedIn profils à partir d'un CSV d'URLs (step 2).")
    parser.add_argument("--csv", type=str, required=True, help="Chemin du CSV d'entrée (colonnes: nom, URL, status, score...).")
    parser.add_argument("--out", type=str, default=None, help="Chemin du JSONL de sortie (optionnel).")
    parser.add_argument("--tag", type=str, default=None, help="Tag inclus dans le nom (ex: client/pays).")
    parser.add_argument("--limit", type=int, default=None, help="Limiter le nombre de profils (debug).")
    parser.add_argument("--save-every", type=int, default=10, help="Sauvegarde incrémentale toutes les N lignes.")
    args = parser.parse_args()

    run_coro(main(Path(args.csv), Path(args.out) if args.out else None, args.tag, args.limit, args.save_every))
