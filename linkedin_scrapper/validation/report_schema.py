# -*- coding: utf-8 -*-
from __future__ import annotations
import pandas as pd
from typing import List

COLUMNS: List[str] = [
    "Nom et Prénom du titulaire",
    "URL du profil",
    "Qualification d'origine (dernière certification ou diplÃ´me)",
    "Dernier métier exercé",
    "Nom de l'entreprise si actif occupé",
    "Durée de l'expérience précédente (en années)",
    "Situation avant le cursus certifiant ou à vocation professionnelle",
    "Situation après la certification (+ 6mois)",
    "Intitulé de poste occupé ou de l'activité indépendante (+ 6mois)",
    "Exerce le métier visé par la certification (+ 6mois)",
    "Type de contrat ou de statut (+ 6mois)",
    "Nom de l'entreprise (+ 6mois)",
    "Situation après la certification (+ 18mois)",
    "Intitulé de poste occupé ou de l'activité indépendante (+ 18mois)",
    "Exerce le métier visé par la certification (+ 18mois)",
    "Type de contrat ou de statut (+ 18mois)",
    "Nom de l'entreprise (+ 18mois)",
]

def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute les colonnes manquantes et impose l'ordre contractuel."""
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[COLUMNS]


