# main.py
# -*- coding: utf-8 -*-
"""
Orchestrateur complet :
Step 1 (URL finder) -> Step 2 (scrape profils LinkedIn) -> Step 3 (reporting final)

USAGE (depuis la racine) :
  # 1) pipeline complète en CLI
  python -m linkedin_scrapper.main --names-csv data/inputs/noms.csv --save-intermediate --tag client_x --ref-date 2022-02-02

  # 2) avec un JSON de paramètres
  python -m linkedin_scrapper.main --config linkedin_scrapper/config/params_run.json

  # 3) JSON + override partiel en CLI (CLI > JSON)
  python -m linkedin_scrapper.main --config linkedin_scrapper/config/params_run.json --limit-step2 5 --skip-step3
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Any, Dict

from linkedin_scrapper.session_settings import (
    OUTPUTS_DIR,
    PROFILES_DIR,
    STORAGE_STATE,   # chemin du fichier JSON de session
    TEMP_DIR,
    TEMP_STEP1_DIR,
)
from linkedin_scrapper.utils_scripts.utils_async import run_coro
from linkedin_scrapper.utils_scripts.housekeeping import purge_old_files, summarize_outputs
from linkedin_scrapper.logging_setup import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

# === Step 1 ===
try:
    from linkedin_scrapper.step1_urls_finder.main_urls_finder import pipeline as step1_pipeline
except Exception as e:
    step1_pipeline = e  # on stocke l'exception pour affichage détaillé

# === Step 2 ===
try:
    from linkedin_scrapper.step2_profile_scraper.scrape_profiles import main as step2_main
except Exception as e:
    step2_main = e

# === Step 3 ===
try:
    from linkedin_scrapper.step3_transform_reporting import main as step3_main
except Exception as e:
    step3_main = e


def _ensure_imports() -> None:
    """Vérifie que les imports dynamiques sont bien chargés, sinon remonte un message clair."""
    missing = []
    details = []

    if isinstance(step1_pipeline, Exception):
        missing.append("linkedin_scrapper.step1_urls_finder.main_urls_finder.pipeline")
        details.append(f"Step1 import error: {repr(step1_pipeline)}")
    if isinstance(step2_main, Exception):
        missing.append("linkedin_scrapper.step2_profile_scraper.scrape_profiles.main")
        details.append(f"Step2 import error: {repr(step2_main)}")
    if isinstance(step3_main, Exception):
        missing.append("linkedin_scrapper.step3_transform_reporting.main")
        details.append(f"Step3 import error: {repr(step3_main)}")

    if missing:
        raise ImportError(
            "Modules manquants :\n - " + "\n - ".join(missing) +
            "\nDétails :\n - " + "\n - ".join(details) +
            "\nVérifie les imports 'linkedin_scrapper.*', la présence des __init__.py et l'encodage UTF-8 des fichiers."
        )


def run_step1(names_csv: Optional[Path], step1_out: Optional[Path], save_intermediate: bool) -> Path:
    """Lance l'étape 1 (URL finder) et retourne le chemin du CSV produit."""
    if step1_out:
        logger.info("[Step1] Skip (sortie existante fournie) -> %s", step1_out)
        return step1_out
    if names_csv is None:
        raise ValueError("[Step1] --names-csv est requis si --step1-out n'est pas fourni.")
    logger.info("[Step1] Lancement URL finder sur : %s", names_csv)
    out_csv = step1_pipeline(Path(names_csv), None, save_intermediate)  # type: ignore[operator]
    logger.info("[Step1] OK -> %s", out_csv)
    return out_csv


def run_step2(step1_csv: Path, tag: Optional[str], limit_step2: Optional[int], save_every: int) -> Path:
    """Lance l'étape 2 (scrape profils) et retourne le chemin du JSONL produit."""
    logger.info("[Step2] Scrape profils depuis : %s", step1_csv)
    out_jsonl = run_coro(step2_main(step1_csv, None, tag, limit_step2, save_every))  # type: ignore[operator]
    logger.info("[Step2] OK -> %s", out_jsonl)
    return out_jsonl


def run_step3(step2_jsonl_or_csv: Optional[Path], ref_date: Optional[str]) -> Path:
    """Lance l'étape 3 (reporting) et retourne le chemin du CSV produit."""
    if step2_jsonl_or_csv:
        logger.info("[Step3] Reporting depuis : %s", step2_jsonl_or_csv)
        out_csv = step3_main(str(step2_jsonl_or_csv), None, ref_date)  # type: ignore[operator]
    else:
        logger.info("[Step3] Reporting depuis le DERNIER fichier de step2 (auto-detect)")
        out_csv = step3_main(None, None, ref_date)  # type: ignore[operator]
    logger.info("[Step3] OK -> %s", out_csv)
    return out_csv


def main(
    names_csv: Optional[Path],
    step1_out: Optional[Path],
    step2_in: Optional[Path],
    skip_step1: Optional[bool],
    skip_step2: Optional[bool],
    skip_step3: Optional[bool],
    save_intermediate: Optional[bool],
    tag: Optional[str],
    limit_step2: Optional[int],
    save_every: Optional[int],
    ref_date: Optional[str],
) -> None:
    """Orchestre les 3 étapes selon les options fournies."""
    _ensure_imports()

    # Diagnostic storage_state (présent / absent)
    try:
        logger.info("[Session] STORAGE_STATE=%s (exists=%s)", STORAGE_STATE, Path(STORAGE_STATE).exists())
    except Exception:
        pass

    # Valeurs par défaut si toujours None après fusion (cohérentes avec le script original)
    skip_step1 = False if skip_step1 is None else skip_step1
    skip_step2 = False if skip_step2 is None else skip_step2
    skip_step3 = False if skip_step3 is None else skip_step3
    save_intermediate = False if save_intermediate is None else save_intermediate
    save_every = 10 if save_every is None else save_every

    produced_step1_csv: Optional[Path] = None
    produced_step2_jsonl: Optional[Path] = None

    try:
        # === STEP 1
        if skip_step1:
            logger.info("[Step1] Skippé")
            if not step1_out and not step2_in:
                raise ValueError("Tu as skippé Step1 : fournis --step1-out (CSV de Step1) ou --step2-in (JSONL/CSV de Step2).")
        else:
            produced_step1_csv = run_step1(names_csv, step1_out, save_intermediate)

        # === STEP 2
        if skip_step2:
            logger.info("[Step2] Skippé")
            if not step2_in and produced_step1_csv is None:
                raise ValueError("Tu as skippé Step2 : fournis --step2-in (JSONL/CSV de Step2) ou lance Step1 pour produire un CSV.")
            if step2_in:
                produced_step2_jsonl = Path(step2_in)
        else:
            if step2_in:
                logger.info("[Step2] Entrée fournie (--step2-in). On saute Step2 et on utilisera ça pour Step3 : %s", step2_in)
                produced_step2_jsonl = Path(step2_in)
            else:
                base_csv = produced_step1_csv or step1_out
                if base_csv is None:
                    raise ValueError("Aucune entrée Step2 disponible. Fournis --step1-out ou --step2-in.")
                produced_step2_jsonl = run_step2(base_csv, tag, limit_step2, save_every)

        # === STEP 3
        if skip_step3:
            logger.info("[Step3] Skippé")
        else:
            if produced_step2_jsonl is None:
                raise ValueError("Step3 n'a pas d'entrée. Assure-toi que Step2 a produit un fichier ou passe --step2-in.")
            run_step3(Path(produced_step2_jsonl), ref_date)

    finally:
        #  Nettoyage session — on CONSERVE le storage_state pour stabiliser la confiance LinkedIn
        try:
            p = Path(STORAGE_STATE)
            if p.exists():
                logger.info("[cleanup] Session conservée : %s", p)
            else:
                logger.info("[cleanup] Aucun fichier de session présent : %s", p)
        except Exception as e:
            logger.warning("[cleanup] Vérif session a échoué : %s", e, exc_info=True)

        #  Purge + résumé
        try:
            purge_old_files(
                dirs=[TEMP_DIR, TEMP_STEP1_DIR, PROFILES_DIR, OUTPUTS_DIR / "reporting"],
                glob_patterns=("*.csv", "*.jsonl"),
                older_than_days=7,
                keep_at_least=5,
            )
        except Exception as e:
            logger.warning("[cleanup] purge_old_files a échoué : %s", e, exc_info=True)

        try:
            summarize_outputs(OUTPUTS_DIR, PROFILES_DIR)
        except Exception as e:
            logger.warning("[summary] Impossible d'afficher le résumé : %s", e, exc_info=True)


# ---------------------------
# Entrée CLI + JSON de config
# ---------------------------

def _load_json_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Fichier de configuration introuvable : {p}")
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Le fichier JSON de configuration doit contenir un objet/dictionnaire.")
    return data


def _coalesce(cli_val, cfg_val):
    """Retourne la valeur CLI si explicitement fournie, sinon celle du JSON, sinon None."""
    return cli_val if cli_val is not None else cfg_val


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Orchestrateur Step1 -> Step2 -> Step3")

    # Fichier de config JSON
    parser.add_argument(
        "--config", type=str, default=None,
        help="Chemin d'un fichier JSON de paramètres. Les arguments CLI fournis priment sur le JSON."
    )

    # Entrées (par défaut None pour permettre la fusion)
    parser.add_argument("--names-csv", type=str, default=None, help="CSV d'entrée pour Step1 (noms/keywords).")
    parser.add_argument("--step1-out", type=str, default=None, help="CSV sortie déjà produit par Step1 (pour reprendre).")
    parser.add_argument("--step2-in", type=str, default=None, help="JSONL/CSV déjà produit par Step2 (pour reprendre).")

    # Skips (tri-état via double option pour détecter l'intention ; défaut = None)
    parser.add_argument("--skip-step1", dest="skip_step1", action="store_true", help="Sauter l'étape 1.")
    parser.add_argument("--no-skip-step1", dest="skip_step1", action="store_false", help="Ne pas sauter l'étape 1.")
    parser.set_defaults(skip_step1=None)

    parser.add_argument("--skip-step2", dest="skip_step2", action="store_true", help="Sauter l'étape 2.")
    parser.add_argument("--no-skip-step2", dest="skip_step2", action="store_false", help="Ne pas sauter l'étape 2.")
    parser.set_defaults(skip_step2=None)

    parser.add_argument("--skip-step3", dest="skip_step3", action="store_true", help="Sauter l'étape 3.")
    parser.add_argument("--no-skip-step3", dest="skip_step3", action="store_false", help="Ne pas sauter l'étape 3.")
    parser.set_defaults(skip_step3=None)

    # Options Step1
    parser.add_argument("--save-intermediate", dest="save_intermediate", action="store_true",
                        help="(Step1) Sauvegarder les fichiers intermédiaires.")
    parser.add_argument("--no-save-intermediate", dest="save_intermediate", action="store_false",
                        help="(Step1) Ne pas sauvegarder les fichiers intermédiaires.")
    parser.set_defaults(save_intermediate=None)

    # Options Step2
    parser.add_argument("--tag", type=str, default=None, help="(Step2) Tag pour nommer la sortie profils.")
    parser.add_argument("--limit-step2", type=int, default=None, help="(Step2) Limiter le nombre de profils (debug).")
    parser.add_argument("--save-every", type=int, default=None, help="(Step2) Sauvegarde JSONL toutes les N lignes.")

    # Option Step3
    parser.add_argument("--ref-date", type=str, default=None,
                        help="(Step3) Date de référence pour M+6 / M+18 (YYYY-MM-DD, DD/MM/YYYY, YYYY/MM/DD).")

    args = parser.parse_args()

    # Charger le JSON s'il est fourni
    cfg = _load_json_config(args.config)

    # Fusion CLI > JSON
    names_csv = _coalesce(args.names_csv, cfg.get("names_csv"))
    step1_out = _coalesce(args.step1_out, cfg.get("step1_out"))
    step2_in = _coalesce(args.step2_in, cfg.get("step2_in"))

    skip_step1 = _coalesce(args.skip_step1, cfg.get("skip_step1"))
    skip_step2 = _coalesce(args.skip_step2, cfg.get("skip_step2"))
    skip_step3 = _coalesce(args.skip_step3, cfg.get("skip_step3"))

    save_intermediate = _coalesce(args.save_intermediate, cfg.get("save_intermediate"))
    tag = _coalesce(args.tag, cfg.get("tag"))
    limit_step2 = _coalesce(args.limit_step2, cfg.get("limit_step2"))
    save_every = _coalesce(args.save_every, cfg.get("save_every"))
    ref_date = _coalesce(args.ref_date, cfg.get("ref_date"))

    # Conversion en Path quand présent
    names_csv_p = Path(names_csv) if names_csv else None
    step1_out_p = Path(step1_out) if step1_out else None
    step2_in_p = Path(step2_in) if step2_in else None

    main(
        names_csv=names_csv_p,
        step1_out=step1_out_p,
        step2_in=step2_in_p,
        skip_step1=skip_step1,
        skip_step2=skip_step2,
        skip_step3=skip_step3,
        save_intermediate=save_intermediate,
        tag=tag,
        limit_step2=limit_step2,
        save_every=save_every,
        ref_date=ref_date,
    )
