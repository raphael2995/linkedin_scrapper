# step2_profile_scraper/constants.py
# -*- coding: utf-8 -*-
"""
Constantes et motifs réutilisables pour l'extraction LinkedIn.
"""

import re

# Termes de contrat FR (+ variantes)
CONTRACT_TERMS = [
    r"CDI",
    r"CDD",
    r"Stage",
    r"Alternance",
    r"Int[ée]rim(?:aire)?",      # Intérim / Interim / Intérimaire
    r"Freelance",
    r"Temps[ -]?plein",          # Temps plein / Temps-plein
    # (Optionnel) Anglais :
    # r"Full[- ]?time",
    # r"Intern(?:ship)?",
    # r"Contract(?:or)?",
    # r"Temp(?:orary)?",
    # r"Apprenticeship",
]

CONTRACT_REGEX_STR = r"\b(" + "|".join(CONTRACT_TERMS) + r")\b"
CONTRACT_REGEX = re.compile(CONTRACT_REGEX_STR, flags=re.IGNORECASE)

__all__ = ["CONTRACT_TERMS", "CONTRACT_REGEX_STR", "CONTRACT_REGEX"]


