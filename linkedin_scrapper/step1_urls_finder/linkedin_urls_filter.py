# -*- coding: utf-8 -*-
"""
Created on Tue Jul  8 16:02:38 2025

@author: Raphael
"""
import re
import pandas as pd
import unicodedata
import urllib.parse
from rapidfuzz import fuzz


# Variables


def remove_accents(text: str) -> str:
    """
    Remove accents from a string using Unicode normalization.

    Args:
        text (str): The input text.

    Returns:
        str: Text without accents.
    """
    return ''.join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )


def preprocess_url_name(url: str) -> str:
    """
    Extract and normalize the name portion from a LinkedIn profile URL.

    Args:
        url (str): The LinkedIn profile URL.

    Returns:
        str: A cleaned and normalized version of the name in the URL.
    """
    # Decode URL-encoded characters (e.g., %C3%A9 → é)
    url = urllib.parse.unquote(url)

    # Remove LinkedIn base URL
    url = re.sub(r'https://(?:\w+\.)?linkedin\.com/in/', '', url)

    # Replace dashes with spaces
    url = url.replace('-', ' ')

    # Remove punctuation, numbers, and normalize whitespace
    url = re.sub(r'[^\w\s]', '', url)         # remove punctuation
    url = re.sub(r'\d+', '', url)             # remove numbers
    url = re.sub(r'\s+', ' ', url).strip()    # normalize whitespace

    # Remove accents and lowercase
    return remove_accents(url).lower()


def preprocess_name(name: str) -> str:
    """
    Normalize a person's name: remove accents, lowercase.

    Args:
        name (str): The original name.

    Returns:
        str: Normalized name.
    """
    return remove_accents(name).lower().strip()


def score_similarity(name: str, url: str) -> float:
    """
    Compute a similarity score between a person's name and a LinkedIn profile URL.

    Handles normal spacing and cases where the name is fused (e.g., 'maloherry').

    Args:
        name (str): Full name (e.g., 'Herry Malo').
        url (str): LinkedIn profile URL.

    Returns:
        float: Similarity score between 0 and 1.
    """
    # Preprocess both inputs
    cleaned_name = preprocess_name(name)
    url_name = preprocess_url_name(url)

    # Case 1: the LinkedIn URL is a single word (e.g., 'maloherry')
    if not re.search(r'\s', url_name):
        # Remove spaces from name → "herrymalo"
        name_fused = cleaned_name.replace(' ', '')

        # Reverse the order → "malo herry" → "maloherry"
        name_reversed = ' '.join(cleaned_name.split()[::-1]).replace(' ', '')

        # Compute max score between original and reversed
        score =  max(
            fuzz.token_set_ratio(url_name, name_fused),
            fuzz.token_set_ratio(url_name, name_reversed)
        ) / 100.0

    else :
        # Case 2: normal spaced name in URL (e.g., "malo herry")
        score =  fuzz.token_set_ratio(url_name, cleaned_name) / 100.0
    
    return round(score, 1)
        

def filter_url(name: str, url: str) -> str:
    if isinstance(url, str) and "linkedin.com" in url:
        return score_similarity(name, url)
    else :
        return 0
    


def split_df_by_confidence(
    df: pd.DataFrame,
    search_index: int,
    confidence_threshold: float = 0.9
) :
    """
    Calcule un score pour chaque ligne via `filter_url(nom, URL)`, enregistre ce score
    dans une colonne dédiée, puis renvoie deux DataFrames :
      - valid_df    : lignes dont le score <  confidence_threshold  (URLs considérées "validées")
      - doubtful_df : lignes dont le score >= confidence_threshold  (URLs à vérifier / douteuses)

    Paramètres
    ----------
    df : pd.DataFrame
        Doit contenir au minimum les colonnes 'nom' et 'URL'.
    search_index : int
        Suffixe/indice utilisé pour nommer la colonne de score (ex: 'score_3').
    confidence_threshold : float, par défaut 0.9
        Seuil séparant les cas "validés" des cas "douteux".

    Retour
    ------
    (valid_df, doubtful_df) : Tuple[pd.DataFrame, pd.DataFrame]
    """
    # Vérifications minimales des colonnes requises
    required_columns = {"nom", "URL"}
    missing = required_columns - set(df.columns)
    if missing:
        raise KeyError(f"Colonnes manquantes dans df : {', '.join(sorted(missing))}")

    # Travailler sur une copie pour éviter les effets de bord
    df_copy = df.copy()

    # 1) Calcul du score via la fonction externe `filter_url(nom, URL)`
    score_col = f"score_{search_index}"
    df_copy[score_col] = df_copy.apply(lambda row: filter_url(row["nom"], row["URL"]), axis=1)


    # 2) Découpage selon le seuil de confiance
    valid_df = df_copy[df_copy[score_col] >= confidence_threshold].copy()
    doubtful_df = df_copy[df_copy[score_col] < confidence_threshold].copy()

    return valid_df, doubtful_df



