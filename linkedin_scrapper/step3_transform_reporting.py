# step3_transform_reporting.py
# -*- coding: utf-8 -*-
"""
Transforme les sorties Step2 (JSONL/CSV avec listes 'experiences' et 'formations')
en un reporting structuré :
- Qualification d'origine (hors DataScientest) avant la date de début du cursus
- Situation avant le cursus (dernier emploi)
- Situation à +6 mois et +18 mois (emploi en cours aux dates de reference)
- Contrat, poste, entreprise

Toutes les dates sont normalisées au 1er du mois, et les comparaisons
se font à la granularité "mois".
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from datetime import datetime, date
import pandas as pd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

from linkedin_scrapper.session_settings import PROFILES_DIR, REPORTING_DIR, timestamp_min
from linkedin_scrapper.logging_setup import get_logger
from linkedin_scrapper.validation.report_schema import ensure_columns



logger = get_logger(__name__)

# =========================
# 0) Paramètres / Helpers
# =========================

DEFAULT_REFERENCE_START = datetime(2022, 2, 2)  # fallback si rien n'est fourni


def _parse_ref_date(s: str) -> datetime:
    """
    Parse une date de référence. Retourne le 1er du mois correspondant.
    Formats acceptés : YYYY-MM-DD | DD/MM/YYYY | YYYY/MM/DD
    """
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            return datetime(d.year, d.month, 1)  # 1er du mois
        except ValueError:
            pass
    raise ValueError(f"Format de date non reconnu pour --ref-date / REF_START : '{s}'")


def _resolve_reference_start(cli_ref_date: Optional[str]) -> datetime:
    """
    Priorité : 1) --ref-date, 2) env REF_START, 3) DEFAULT_REFERENCE_START
    (toutes normalisées au 1er du mois)
    """
    if cli_ref_date:
        return _parse_ref_date(cli_ref_date)
    env_val = os.getenv("REF_START")
    if env_val:
        return _parse_ref_date(env_val)
    d = DEFAULT_REFERENCE_START
    return datetime(d.year, d.month, 1)


def _as_month(d: Optional[datetime | date]) -> Optional[date]:
    """Ramene une date (année, mois), normalisée au 1er du mois."""
    if d is None:
        return None
    return date(d.year, d.month, 1)


def _ym_tuple(d: date) -> tuple[int, int]:
    """Tuple (année, mois) pour comparaison lexicographique."""
    return (d.year, d.month)


def _months_between(start_m: date, end_m: date) -> int:
    """Nombre de mois entre deux dates normalisées au 1er du mois (start <= end)."""
    return (end_m.year - start_m.year) * 12 + (end_m.month - start_m.month)


def _m_plus(reference: datetime, months: int) -> datetime:
    """Retourne le 1er du mois de (référence + N mois)."""
    ref_m = _as_month(reference) or date(reference.year, reference.month, 1)
    new_m = ref_m + relativedelta(months=+months)
    return datetime(new_m.year, new_m.month, 1)


# Mois FR → EN (pour strptime)
FRENCH_MONTHS = {
    "janv.": "Jan", "févr.": "Feb", "mars": "Mar", "avr.": "Apr", "mai": "May",
    "juin": "Jun", "juil.": "Jul", "aout": "Aug", "sept.": "Sep", "oct.": "Oct",
    "nov.": "Nov", "déc.": "Dec",
    "janvier": "January", "février": "February", "avril": "April", "juillet": "July",
    "septembre": "September", "octobre": "October", "novembre": "November", "décembre": "December",
}

# =========================
# 1) Chargement des données
# =========================

def _latest_profiles_path() -> Path:
    """Retourne le dernier JSONL/CSV généré par le step2 (dans PROFILES_DIR)."""
    candidates = list(Path(PROFILES_DIR).glob("profiles_*.jsonl")) + list(Path(PROFILES_DIR).glob("profiles_*.csv"))
    if not candidates:
        raise FileNotFoundError(f"Aucun fichier 'profiles_*.jsonl/csv' trouvé dans {PROFILES_DIR}")
    return max(candidates, key=lambda p: p.stat().st_mtime)

def _load_profiles(path: Path) -> pd.DataFrame:
    """
    Charge un JSONL (préféré) ou CSV produit par le step2.
    Attend les colonnes: nom/Nom, URL/url, experiences, formations.
    """
    if path.suffix.lower() == ".jsonl":
        df = pd.read_json(path, lines=True)
    elif path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype="string").fillna("")
        # Convertir les colonnes 'experiences' / 'formations' de string JSON → list
        for col in ("experiences", "formations"):
            if col in df.columns:
                df[col] = df[col].apply(lambda s: _safe_parse_list(s))
    else:
        raise ValueError("Format non supporté (attendu .jsonl ou .csv)")
    return df.fillna("")

def _safe_parse_list(s: Any) -> List[Dict[str, Any]]:
    """Convertit une chaine JSON / repr Python en liste de dicts."""
    if isinstance(s, list):
        return s
    if not isinstance(s, str) or s.strip() == "":
        return []
    try:
        return json.loads(s)
    except Exception:
        try:
            return ast.literal_eval(s)
        except Exception:
            return []

# =========================
# 2) Parsing des dates (mois)
# =========================

def parse_french_date(date_str: str) -> Optional[datetime]:
    """Convertit 'aout 2021' en datetime normalisé au 1er du mois. Retourne None si échec."""
    if not isinstance(date_str, str) or not date_str.strip():
        return None
    s = date_str.strip()
    for fr, en in FRENCH_MONTHS.items():
        s = s.replace(fr, en)
    for fmt in ("%b %Y", "%B %Y"):
        try:
            d = datetime.strptime(s, fmt)
            return datetime(d.year, d.month, 1)  # 1er du mois
        except ValueError:
            continue
    return None

def parse_year_range(date_str: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Traite '2015 - 2017' ou '2018' → (1er jan, 1er déc) en granularité mois."""
    import re
    if not isinstance(date_str, str):
        return None, None
    s = date_str.replace("â€“", "-").strip()
    years = re.findall(r"\d{4}", s)
    if len(years) == 2:
        return datetime(int(years[0]), 1, 1), datetime(int(years[1]), 12, 1)
    if len(years) == 1:
        y = int(years[0])
        return datetime(y, 1, 1), datetime(y, 12, 1)
    return None, None

def extract_date_range(date_str: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Extrait (début, fin) depuis 'aout 2021 - aujourd’hui Â· 2 ans', 'janv. 2020 - juin 2022', etc.
    Retourne des datetime normalisés au 1er du mois.
    """
    if not isinstance(date_str, str):
        return None, None
    head = date_str.split("Â·")[0].strip()  # on enlève la durée 'Â· 2 ans'
    parts = [p.strip() for p in head.split(" - ")]
    start = parse_french_date(parts[0]) if parts else None
    if len(parts) > 1:
        if "aujourd’hui" in parts[1].lower() or "aujourdhui" in parts[1].lower():
            now_m = _as_month(datetime.now())
            end = datetime(now_m.year, now_m.month, 1) if now_m else None
        else:
            end = parse_french_date(parts[1]) or None
    else:
        end = None
    if not start and not end:
        # fallback pour les formations type '2018 - 2020'
        return parse_year_range(date_str)

    # Normalisation au mois (sécurité)
    start_m = _as_month(start)
    end_m = _as_month(end) if end else None
    start = datetime(start_m.year, start_m.month, 1) if start_m else None
    end = datetime(end_m.year, end_m.month, 1) if end_m else None
    return start, end

# =========================
# 3) Sélection des expériences / formations (comparaisons au mois)
# =========================

def experience_active_at(experiences: List[Dict[str, Any]], ref_date: datetime) -> Optional[Dict[str, Any]]:
    """
    Retourne l'expérience en cours au MOIS de ref_date (la plus récente par date de début).
    Attend des items avec clés: 'titre', 'entreprise', 'dates', ('type contrat' ou 'contrat').
    """
    best = None
    ref_m = _as_month(ref_date) or date(ref_date.year, ref_date.month, 1)
    for exp in experiences or []:
        start, end = extract_date_range(exp.get("dates", ""))
        if not start:
            continue
        start_m = _as_month(start)
        end_m = _as_month(end) or _as_month(datetime.now())
        if not start_m or not end_m:
            continue

        if _ym_tuple(start_m) <= _ym_tuple(ref_m) <= _ym_tuple(end_m):
            if (best is None) or (_ym_tuple(start_m) > _ym_tuple(_as_month(best["start"]))):
                best = {
                    "titre": exp.get("titre", ""),
                    "entreprise": exp.get("entreprise", ""),
                    "contrat": exp.get("type contrat", "") or exp.get("contrat", ""),
                    "start": datetime(start_m.year, start_m.month, 1),
                    "end": datetime(end_m.year, end_m.month, 1),
                }
    return best

def last_experience_before(experiences: List[Dict[str, Any]], ref_date: datetime) -> Optional[Dict[str, Any]]:
    """
    Dernière expérience ayant commencé AVANT le MOIS de ref_date.
    Choix = celle dont la fin (ou mois courant si en cours) est la plus récente avant ref_date.
    """
    best = None
    ref_m = _as_month(ref_date) or date(ref_date.year, ref_date.month, 1)
    for exp in experiences or []:
        start, end = extract_date_range(exp.get("dates", ""))
        if not start:
            continue
        start_m = _as_month(start)
        end_m = _as_month(end) or _as_month(datetime.now())
        if not start_m or not end_m:
            continue

        if _ym_tuple(start_m) >= _ym_tuple(ref_m):
            continue  # commence au mÃªme mois ou après la référence → on ignore

        cand = {
            "titre": exp.get("titre", ""),
            "entreprise": exp.get("entreprise", ""),
            "contrat": exp.get("type contrat", "") or exp.get("contrat", ""),
            "start": datetime(start_m.year, start_m.month, 1),
            "end": datetime(end_m.year, end_m.month, 1),
        }
        if (best is None) or (_ym_tuple(_as_month(cand["end"])) > _ym_tuple(_as_month(best["end"]))):
            best = cand

    if best:
        s_m = _as_month(best["start"])
        e_m = _as_month(best["end"])
        months = _months_between(s_m, e_m)
        best["duree_annees"] = months // 12  # années entières
        best["duree_mois"] = months
        best["duree_label"] = f"{months//12} an(s) {months%12} mois"
    return best

def last_non_ds_qualification(formations: List[Dict[str, Any]], ref_date: datetime) -> Optional[Dict[str, Any]]:
    """Dernière formation avant le MOIS de ref_date, dont l'organisme ne contient pas 'datascientest'."""
    best = None
    ref_m = _as_month(ref_date) or date(ref_date.year, ref_date.month, 1)
    for f in formations or []:
        org = str(f.get("organisme", "")).lower()
        if "datascientest" in org:
            continue
        start, end = extract_date_range(f.get("dates", ""))
        if not end:
            start, end = parse_year_range(f.get("dates", ""))

        start_m = _as_month(start) if start else None
        end_m = _as_month(end) if end else None
        if not end_m:
            continue  # on veut une fin connue
        if _ym_tuple(end_m) >= _ym_tuple(ref_m):
            continue  # formation se termine au mÃªme mois ou après la référence

        cand = {
            "titre": f.get("titre", ""),
            "organisme": f.get("organisme", ""),
            "start": datetime(start_m.year, start_m.month, 1) if start_m else None,
            "end": datetime(end_m.year, end_m.month, 1),
        }
        if (best is None) or (_ym_tuple(_as_month(cand["end"])) > _ym_tuple(_as_month(best["end"]))):
            best = cand
    return best

# =========================
# 4) Construction du reporting
# =========================

def build_output_row(row: Dict[str, Any], reference_start: datetime) -> Dict[str, Any]:
    """
    Construit une ligne du reporting à partir d’une ligne DF.
    Ajoute les rubriques à +6 mois ET +18 mois (comparaisons au mois).

    Attention :
    - Situation/Entreprise "avant le cursus" = basées sur l'expérience EN COURS à la date de référence.
    - Dernier métier exercé = expérience en cours si elle existe, sinon la dernière avant la référence.
    """
    name = row.get("nom") or row.get("Nom") or ""
    profile_url = row.get("URL") or row.get("url") or ""
    experiences = row.get("experiences") or []
    formations = row.get("formations") or []

    # Jalons (au mois)
    six_month_date = _m_plus(reference_start, 6)
    eighteen_month_date = _m_plus(reference_start, 18)

    # Sélections "avant le cursus"
    exp_before = last_experience_before(experiences, reference_start)      # dernier job avant la ref
    exp_at_ref = experience_active_at(experiences, reference_start)        # job en cours à la ref
    qualif = last_non_ds_qualification(formations, reference_start)

    # Dérivés "avant cursus"
    dernier_metier = (exp_at_ref or exp_before or {}).get("titre", "")
    entreprise_si_actif = (exp_at_ref or {}).get("entreprise", "")
    situation_avant = "Actif occupé" if exp_at_ref else "En recherche d'emploi"

    # états à +6 et +18 mois
    exp_m6 = experience_active_at(experiences, six_month_date)
    exp_m18 = experience_active_at(experiences, eighteen_month_date)

    return {
        "Nom et Prénom du titulaire": name or "sans réponse",
         "URL du profil": profile_url or "sans réponse",

        # Avant cursus
        "Qualification d'origine (dernière certification ou diplÃ´me)": (qualif or {}).get("titre", "") or "sans réponse",
        "Dernier métier exercé": dernier_metier or "sans réponse",
        "Nom de l'entreprise si actif occupé": entreprise_si_actif or "sans réponse",
        "Durée de l'expérience précédente (en années)": (exp_before or {}).get("duree_annees", "sans réponse"),
        "Situation avant le cursus certifiant ou à vocation professionnelle": situation_avant,

        # +6 mois
        "Situation après la certification (+ 6mois)": (
            "En poste (ou actif occupé hors alternance)" if exp_m6 else "En recherche d'emploi"
        ),
        "Intitulé de poste occupé ou de l'activité indépendante (+ 6mois)": (exp_m6 or {}).get("titre", "") or "sans réponse",
        "Exerce le métier visé par la certification (+ 6mois)": "Oui" if exp_m6 else "Non",
        "Type de contrat ou de statut (+ 6mois)": (exp_m6 or {}).get("contrat", "") or "sans réponse",
        "Nom de l'entreprise (+ 6mois)": (exp_m6 or {}).get("entreprise", "") or "sans réponse",

        # +18 mois
        "Situation après la certification (+ 18mois)": (
            "En poste (ou actif occupé hors alternance)" if exp_m18 else "En recherche d'emploi"
        ),
        "Intitulé de poste occupé ou de l'activité indépendante (+ 18mois)": (exp_m18 or {}).get("titre", "") or "sans réponse",
        "Exerce le métier visé par la certification (+ 18mois)": "Oui" if exp_m18 else "Non",
        "Type de contrat ou de statut (+ 18mois)": (exp_m18 or {}).get("contrat", "") or "sans réponse",
        "Nom de l'entreprise (+ 18mois)": (exp_m18 or {}).get("entreprise", "") or "sans réponse",
    }

# =========================
# 5) CLI
# =========================

def _default_out() -> Path:
    """
    CSV par défaut dans le dossier unifié:
      data/outputs/reporting/reporting_<YYYYMMDD_HHMM>.csv
    """
    REPORTING_DIR.mkdir(parents=True, exist_ok=True)
    return REPORTING_DIR / f"reporting_{timestamp_min()}.csv"


def main(in_path: Optional[str], out_path: Optional[str], ref_date: Optional[str] = None) -> Path:
    reference_start = _resolve_reference_start(ref_date)  # déjà normalisée au 1er du mois

    src = Path(in_path) if in_path else _latest_profiles_path()
    df = _load_profiles(src)

    # Construction du reporting
    records = [build_output_row(row, reference_start) for row in df.to_dict(orient="records")]
    df_out = pd.DataFrame(records)
    df_out = ensure_columns(df_out)  # colonnes stables + ordre garanti
    
    out = Path(out_path) if out_path else _default_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Encodage UTF-8-SIG pour ouverture Excel cÃ´té client
    df_out.to_csv(out, index=False, encoding="utf-8-sig")
    logger.info("Reporting exporté : %s (référence=%s)", out, reference_start.strftime("%Y-%m"))
    return out


def export_report(df: pd.DataFrame, out_path: Optional[str] = None) -> Path:
    """
    Petit wrapper réutilisable si tu veux appeler l’export depuis ailleurs.
    Utilise settings.output_csv si out_path n’est pas fourni.
    """
    out = Path(out_path) if out_path else _default_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Step3 - Transformation reporting depuis les sorties Step2 (granularité mois).")
    parser.add_argument("--in", dest="in_path", default=None, help="Chemin du JSONL/CSV source (sinon dernier fichier de step2).")
    parser.add_argument("--out", dest="out_path", default=None, help="Chemin du CSV de sortie (optionnel).")
    parser.add_argument("--ref-date", dest="ref_date", default=None,
                        help="Date de référence (YYYY-MM-DD, DD/MM/YYYY, YYYY/MM/DD) ; sinon REF_START ; sinon 2022-02-02. Normalisée au 1er du mois.")
    args = parser.parse_args()
    main(args.in_path, args.out_path, args.ref_date)



