# -*- coding: utf-8 -*-
"""
Pipeline automatisée de recherche d’URL LinkedIn (Ã‰tape 1).

Ã‰tapes :
1) Google mode 1 : "site:linkedin.com/in {nom} datascientest"
   → filtrage par similarité (seuil 0.90)

2) Google mode 2 : "Linkedin {nom} {keywords}"
   (uniquement pour les noms restants de l’étape 1)
   → filtrage par similarité (seuil 0.90)

3) LinkedIn internal search : mode 3 = "{nom} datascientest"
   ET 4 = "{nom} {keywords}" ENCHAÃŽNÃ‰S DANS LA MÃŠME SESSION
   via entry_batch_modes_3_then_4 (avec pause longue intégrée)
   → filtrage par similarité (seuil 0.90 pour les â€œvalidâ€)

Sélection finale :
- Si un nom a une URL avec score â‰¥ 0.90 sur l’une des étapes, on garde la 1Ê³áµ‰ trouvée
  dans l’ordre de priorité : étape 1 → 2 → 3 → 4.
- Sinon, on regarde toutes ses propositions â‰¥ 0.70 (toutes étapes confondues)
  et on prend la meilleure.
- Si aucune proposition â‰¥ 0.70, on renvoie URL vide et status "no_confident_match".

Sortie :
- CSV final dans data/outputs/step1/ (par défaut) contenant les colonnes d’entrée + URL + status + score.

Usage :
    python -m step1_urls_finder.main_urls_finder \
        --csv data/inputs/noms.csv \
        --save-intermediate
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, List

import pandas as pd

# Chemins & horodatage centralisés
from linkedin_scrapper.session_settings import STEP1_DIR, TEMP_STEP1_DIR, timestamp_min

# Orchestration async
from linkedin_scrapper.utils_scripts.utils_async import run_coro

# Ã‰tape 1 â€“ workers
from linkedin_scrapper.step1_urls_finder import google_searcher as gs
from linkedin_scrapper.step1_urls_finder.linkedin_urls_filter import split_df_by_confidence
from linkedin_scrapper.step1_urls_finder.linkedin_searcher import entry_batch_modes_3_then_4

from linkedin_scrapper.logging_setup import get_logger
logger = get_logger(__name__)

# Colonnes attendues
NAME_COLUMN = "nom"
KW_COLUMN = "keywords"

# Seuils de décision
HIGH_CONF = 0.90
FALLBACK_CONF = 0.70


# --------------------------
# Utilitaires de fichier
# --------------------------
def save_intermediate(df: pd.DataFrame, label: str) -> None:
    """Sauve un CSV intermédiaire (optionnel) dans TEMP_STEP1_DIR."""
    TEMP_STEP1_DIR.mkdir(parents=True, exist_ok=True)
    out = TEMP_STEP1_DIR / f"{label}_{timestamp_min()}.csv"
    try:
        df.to_csv(out, index=False, encoding="utf-8")
        logger.info("Intermédiaire → %s", out)
    except Exception as e:
        logger.warning("Impossible de sauvegarder %s: %s", label, e, exc_info=True)


# --------------------------
# Préparation entrée
# --------------------------
def _ensure_input(df: pd.DataFrame) -> pd.DataFrame:
    """Vérifie et normalise les colonnes d’entrée (nom, keywords)."""
    if NAME_COLUMN not in df.columns:
        raise ValueError(f"Colonne '{NAME_COLUMN}' manquante dans le CSV d'entrée.")
    if KW_COLUMN not in df.columns:
        df[KW_COLUMN] = ""
    df[NAME_COLUMN] = df[NAME_COLUMN].astype("string").fillna("").str.strip()
    df[KW_COLUMN] = df[KW_COLUMN].astype("string").fillna("").str.strip()
    # Supprime lignes sans nom
    df = df[df[NAME_COLUMN] != ""].reset_index(drop=True)
    return df


# --------------------------
# Google wrapper
# --------------------------
def run_google_search(query_mode: int, input_df: pd.DataFrame, temp_csv_path: Path) -> pd.DataFrame:
    """
    Ã‰crit un CSV temporaire pour google_searcher, exécute gs.main(query_mode, csv),
    retourne un DataFrame résultats (au moins colonnes: nom / URL).
    """
    temp_csv_path.parent.mkdir(parents=True, exist_ok=True)
    input_df.to_csv(temp_csv_path, index=False, encoding="utf-8")

    try:
        df_res = run_coro(gs.main(query_mode, str(temp_csv_path)))
    except Exception as e:
        logger.error("google_searcher.main a échoué (mode=%s): %s", query_mode, e, exc_info=True)
        return pd.DataFrame(columns=[NAME_COLUMN, "URL"])

    if df_res is None or len(df_res) == 0:
        return pd.DataFrame(columns=[NAME_COLUMN, "URL"])

    # normalisation douce
    try:
        df_res = df_res.astype("string").fillna("")
    except Exception:
        pass

    # s’assure qu’on a nom / URL
    if NAME_COLUMN not in df_res.columns and "Nom" in df_res.columns:
        df_res[NAME_COLUMN] = df_res["Nom"].astype("string")
    if "URL" not in df_res.columns:
        df_res["URL"] = ""

    # On garde les colonnes utiles et celles d’entrée si présentes
    keep_cols = [c for c in [NAME_COLUMN, "URL", KW_COLUMN, "status"] if c in df_res.columns]
    return df_res[keep_cols].copy()


# --------------------------
# Sélection & fusion finale
# --------------------------
def _add_unified_score(df: pd.DataFrame, step_idx: int) -> pd.DataFrame:
    """
    Copie df (qui contient 'score_{step_idx}') et ajoute :
      - 'score' : alias sur 'score_{step_idx}'
      - 'source_step' : step_idx (1,2,3,4)
    """
    score_col = f"score_{step_idx}"
    if score_col not in df.columns:
        out = df.copy()
        out["score"] = 0.0
        out["source_step"] = step_idx
        return out
    out = df.copy()
    out["score"] = out[score_col].astype(float)
    out["source_step"] = step_idx
    return out


def _prioritized_merge(
    valid1: pd.DataFrame,
    valid2: pd.DataFrame,
    valid3: pd.DataFrame,
    valid4: pd.DataFrame,
    fallback_candidates: pd.DataFrame,
    df_in: pd.DataFrame,
) -> pd.DataFrame:
    """
    Construit le DataFrame final avec priorité :
      1) URLs â‰¥ 0.90 de l’étape 1, puis 2, puis 3, puis 4.
      2) Sinon, meilleure URL â‰¥ 0.70 (toutes étapes confondues).
      3) Sinon, pas d’URL (status no_confident_match).
    """
    final_map: dict[str, dict] = {}

    def _apply_valid(dfv: pd.DataFrame, step_idx: int):
        for row in dfv.itertuples(index=False):
            nom = getattr(row, NAME_COLUMN)
            url = getattr(row, "URL", "")
            score = getattr(row, f"score_{step_idx}", 0.0)
            if not nom:
                continue
            if nom not in final_map:
                final_map[nom] = {
                    "URL": url or "",
                    "score": float(score) if score else 0.0,
                    "status": "ok",
                    "source_step": step_idx,
                }

    # 1) priorité 1→2→3→4
    _apply_valid(valid1, 1)
    _apply_valid(valid2, 2)
    _apply_valid(valid3, 3)
    _apply_valid(valid4, 4)

    # 2) fallback â‰¥ 0.70 si rien encore
    if not fallback_candidates.empty:
        fb_sorted = (
            fallback_candidates
            .sort_values([NAME_COLUMN, "score"], ascending=[True, False])
            .drop_duplicates(subset=[NAME_COLUMN], keep="first")
        )
        for row in fb_sorted.itertuples(index=False):
            nom = getattr(row, NAME_COLUMN)
            if nom in final_map:
                continue
            url = getattr(row, "URL", "")
            score = float(getattr(row, "score", 0.0))
            step_idx = int(getattr(row, "source_step", 0))
            final_map[nom] = {
                "URL": url or "",
                "score": score,
                "status": "fallback_best",
                "source_step": step_idx,
            }

    # 3) Compléter avec â€œpas d’URLâ€
    rows = []
    for row in df_in.itertuples(index=False):
        nom = getattr(row, NAME_COLUMN)
        base = {c: getattr(row, c) for c in df_in.columns}
        if nom in final_map:
            info = final_map[nom]
            base.update(
                URL=info["URL"],
                status=info["status"],
                score=info["score"],
                source_step=info["source_step"],
            )
        else:
            base.update(URL="", status="no_confident_match", score=0.0, source_step=0)
        rows.append(base)

    return pd.DataFrame(rows)


# --------------------------
# Pipeline principale
# --------------------------
def pipeline(input_csv: Path, out_csv: Optional[Path], save_steps: bool) -> Path:
    """
    Pipeline principal :
    - charge l'entrée (nom, keywords)
    - Google mode 1 + filtrage (â‰¥ 0.90)
    - Google mode 2 pour les restants + filtrage (â‰¥ 0.90)
    - LinkedIn modes 3 puis 4 DANS LA MÃŠME SESSION + filtrage (â‰¥ 0.90)
    - sélection finale : priorité 1→2→3→4 ; sinon meilleur â‰¥ 0.70 ; sinon pas d’URL
    - sauvegarde le CSV final (data/outputs/step1/)
    """
    # 0) Charger l’entrée
    df_in = pd.read_csv(input_csv, dtype="string").fillna("")
    df_in = _ensure_input(df_in)

    # 1) Google mode 1
    logger.info("[RECHERCHE 1] Google mode 1")
    g1_raw = run_google_search(
        query_mode=1,
        input_df=df_in[[NAME_COLUMN, KW_COLUMN]].copy(),
        temp_csv_path=TEMP_STEP1_DIR / f"temp_google_mode1_{timestamp_min()}.csv",
    )
    if save_steps:
        save_intermediate(g1_raw, "gsearch_mode1_raw")
    valid1, doubtful1 = split_df_by_confidence(g1_raw, search_index=1, confidence_threshold=HIGH_CONF)
    if save_steps:
        save_intermediate(valid1, "gsearch_mode1_valid")
        save_intermediate(doubtful1, "gsearch_mode1_doubtful")

    # Restants après étape 1
    remaining1 = df_in[~df_in[NAME_COLUMN].isin(valid1[NAME_COLUMN])].copy()

    # 2) Google mode 2 (si restants)
    if remaining1.empty:
        valid2 = pd.DataFrame(columns=[NAME_COLUMN, "URL", "score_2"])
        doubtful2 = pd.DataFrame(columns=[NAME_COLUMN, "URL", "score_2"])
    else:
        logger.info("[RECHERCHE 2] Google mode 2")
        g2_raw = run_google_search(
            query_mode=2,
            input_df=remaining1[[NAME_COLUMN, KW_COLUMN]].copy(),
            temp_csv_path=TEMP_STEP1_DIR / f"temp_google_mode2_{timestamp_min()}.csv",
        )
        if save_steps:
            save_intermediate(g2_raw, "gsearch_mode2_raw")
        valid2, doubtful2 = split_df_by_confidence(g2_raw, search_index=2, confidence_threshold=HIGH_CONF)
        if save_steps:
            save_intermediate(valid2, "gsearch_mode2_valid")
            save_intermediate(doubtful2, "gsearch_mode2_doubtful")

    # Restants après étape 2
    remaining2 = df_in[
        ~df_in[NAME_COLUMN].isin(valid1[NAME_COLUMN]) &
        ~df_in[NAME_COLUMN].isin(valid2[NAME_COLUMN])
    ].copy()

    # 3) LinkedIn internal search (modes 3 puis 4 dans UNE session)
    if remaining2.empty:
        valid3 = pd.DataFrame(columns=[NAME_COLUMN, "URL", "score_3"])
        valid4 = pd.DataFrame(columns=[NAME_COLUMN, "URL", "score_4"])
        doubtful3 = pd.DataFrame(columns=[NAME_COLUMN, "URL", "score_3"])
        doubtful4 = pd.DataFrame(columns=[NAME_COLUMN, "URL", "score_4"])
    else:
        logger.info("[RECHERCHE 3 & 4] LinkedIn internal search â€” modes 3 puis 4 (mÃªme session)")
        li3_raw, li4_raw = run_coro(entry_batch_modes_3_then_4(remaining2))
        if li3_raw is None:
            li3_raw = pd.DataFrame(columns=[NAME_COLUMN, "URL"])
        if li4_raw is None:
            li4_raw = pd.DataFrame(columns=[NAME_COLUMN, "URL"])

        if save_steps:
            save_intermediate(li3_raw, "linkedin_mode3_raw")
            save_intermediate(li4_raw, "linkedin_mode4_raw")

        valid3, doubtful3 = split_df_by_confidence(li3_raw, search_index=3, confidence_threshold=HIGH_CONF)
        valid4, doubtful4 = split_df_by_confidence(li4_raw, search_index=4, confidence_threshold=HIGH_CONF)

        if save_steps:
            save_intermediate(valid3, "linkedin_mode3_valid")
            save_intermediate(doubtful3, "linkedin_mode3_doubtful")
            save_intermediate(valid4, "linkedin_mode4_valid")
            save_intermediate(doubtful4, "linkedin_mode4_doubtful")

    # ----- Sélection finale -----
    # (a) Candidats â€œvalidâ€ â‰¥ 0.90
    v1 = _add_unified_score(valid1, 1)
    v2 = _add_unified_score(valid2, 2)
    v3 = _add_unified_score(valid3, 3)
    v4 = _add_unified_score(valid4, 4)

    # (b) Fallback : tous â€œdoubtfulâ€ â‰¥ 0.70
    fb_list: List[pd.DataFrame] = []
    for step_idx, ddf in [(1, doubtful1), (2, doubtful2), (3, doubtful3), (4, doubtful4)]:
        if ddf is None or ddf.empty:
            continue
        tmp = _add_unified_score(ddf, step_idx)
        fb_list.append(tmp[tmp["score"] >= FALLBACK_CONF])

    fallback_df = (
        pd.concat(fb_list, ignore_index=True)
        if fb_list else pd.DataFrame(columns=[NAME_COLUMN, "URL", "score", "source_step"])
    )

    final_df = _prioritized_merge(v1, v2, v3, v4, fallback_df, df_in)

    # 5) Sauvegarde finale (STEP1_DIR + horodatage sans secondes)
    STEP1_DIR.mkdir(parents=True, exist_ok=True)
    if out_csv is None:
        out_csv = STEP1_DIR / f"pipeline_search_results_{timestamp_min()}.csv"

    final_df.to_csv(out_csv, index=False, encoding="utf-8")
    logger.info("Fichier final : %s", out_csv)
    return out_csv


# --------------------------
# CLI
# --------------------------
def main():
    parser = argparse.ArgumentParser(description="Ã‰tape 1 â€” pipeline de recherche d’URLs LinkedIn.")
    parser.add_argument("--csv", type=str, required=True, help="Chemin du CSV d'entrée (colonnes: nom[, keywords]).")
    parser.add_argument("--out", type=str, default=None, help="Chemin du CSV de sortie (optionnel).")
    parser.add_argument("--save-intermediate", action="store_true", help="Sauvegarder les étapes intermédiaires.")
    args = parser.parse_args()

    input_csv = Path(args.csv)
    out_csv = Path(args.out) if args.out else None
    save_steps = bool(args.save_intermediate)

    pipeline(input_csv, out_csv, save_steps)


if __name__ == "__main__":
    main()



