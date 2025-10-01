# step2_profile_scraper/linkedin_sections_extractor.py
# -*- coding: utf-8 -*-
"""
Extracteurs des sections LinkedIn : Formation & Expériences (avec/sans clic).
- Robustes aux variations FR/EN et aux deux vues (profil / "voir plus").
- Surs : ne lèvent pas d’exception bloquante → renvoient toujours des listes.
- Sélecteurs externalisés (config/selectors.json) via utils_scripts.selectors_registry

Dépendances internes :
- step2_profile_scraper/constants.py pour CONTRACT_REGEX (motif précompilé)
"""

from __future__ import annotations

import re
from typing import List, Dict, Optional

from playwright.async_api import Page

from linkedin_scrapper.step2_profile_scraper.constants import CONTRACT_REGEX  # précompilé
from linkedin_scrapper.utils_scripts.selectors_registry import sel_list, pick_locale


# ======================================================
# Helpers génériques (locale, sélecteurs, texte, scroll)
# ======================================================

async def _locale_hint(page: Page) -> str:
    """Déduit 'fr' / 'en' via navigator.language, mappé par pick_locale()."""
    try:
        nav_lang = await page.evaluate("() => navigator.language || navigator.userLanguage || 'en-US'")
    except Exception:
        nav_lang = "en-US"
    return pick_locale("linkedin", nav_lang)


async def _first_visible(page: Page, selectors: list[str], timeout: int = 3500):
    """Retourne le premier locator VISIBLE parmi une liste de sélecteurs CSS, sinon None."""
    for css in selectors or []:
        try:
            loc = page.locator(css).first
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def _first_text(page: Page, selectors: list[str], timeout: int = 2500) -> str:
    """Texte du premier sélecteur visible (ou '')."""
    el = await _first_visible(page, selectors, timeout=timeout)
    if not el:
        return ""
    try:
        txt = await el.text_content()
        return re.sub(r"\s+", " ", (txt or "").strip())
    except Exception:
        return ""


async def _scroll_to_section(page: Page, section: str, locale: str, timeout: int = 3500) -> bool:
    """
    Essaie de se placer sur la section demandée (experience|education) en utilisant:
    - ancres / data-view-name / aria-label via sélecteurs externalisés "containers"
    - headings FR/EN inclus dans les containers définis cÃ´té JSON
    """
    containers = sel_list("linkedin", "profile", f"sections.{section}.containers", locale=locale)
    el = await _first_visible(page, containers, timeout=timeout)
    if not el:
        return False
    try:
        await el.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)
        return True
    except Exception:
        return False


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


async def _extract_cards(
    page: Page,
    locale: str,
    section_key: str,  # "education" | "experience"
    containers_key: str = "sections",
) -> List[Dict[str, str]]:
    """
    Parcourt la section (containers/items externalisés) et extrait des cartes génériques :
    {title, subtitle, meta, description}. Ne lève pas.
    """
    out: List[Dict[str, str]] = []
    try:
        # 1) Conteneurs + items depuis la config
        containers = sel_list("linkedin", "profile", f"{containers_key}.{section_key}.containers", locale=locale)
        items_sel = sel_list("linkedin", "profile", f"{containers_key}.{section_key}.items",      locale=locale)

        container = await _first_visible(page, containers, timeout=3500)
        root = container if container is not None else page

        items = root.locator(", ".join(items_sel)) if items_sel else root.locator("li")
        count = await items.count()

        # 2) Field selectors depuis la config
        s_title = sel_list("linkedin", "profile", "fields.title",       locale=locale)
        s_sub   = sel_list("linkedin", "profile", "fields.subtitle",    locale=locale)
        s_meta  = sel_list("linkedin", "profile", "fields.meta",        locale=locale)
        s_desc  = sel_list("linkedin", "profile", "fields.description", locale=locale)

        for i in range(count):
            it = items.nth(i)

            # Crée des sélecteurs "scopés" à l'item courant
            def scope(css_list: list[str]) -> list[str]:
                return [f":scope {css}" if not css.startswith(":scope") else css for css in css_list]

            title = await _first_text(it, scope(s_title), timeout=1200)
            subtitle = await _first_text(it, scope(s_sub), timeout=1200)
            meta = await _first_text(it, scope(s_meta), timeout=1200)
            desc = await _first_text(it, scope(s_desc), timeout=1000)

            out.append({
                "title": _norm(title),
                "subtitle": _norm(subtitle),
                "meta": _norm(meta),
                "description": _norm(desc),
            })
    except Exception:
        # sécurité : pas d'exception bloquante
        return out
    return out


# ==================================
# Formation (profil ou "voir plus")
# ==================================
async def extract_formation(page: Page) -> List[Dict[str, str]]:
    """
    Extrait la section 'Formation' (page principale ou vue â€œvoir plusâ€).
    Retour: liste d'objets {organisme (école), titre (diplÃ´me), dates, lieu?}
    """
    results: List[Dict[str, str]] = []
    try:
        loc = await _locale_hint(page)
        await _scroll_to_section(page, "education", loc, timeout=3000)

        rows = await _extract_cards(page, loc, section_key="education")
        for r in rows:
            # Mapping générique → Formation
            results.append({
                "organisme": r.get("subtitle", ""),      # école / établissement
                "titre": r.get("title", ""),             # diplÃ´me
                "dates": r.get("meta", ""),
                "lieu": r.get("description", ""),        # souvent vide ; laissé pour compat
            })
    except Exception:
        pass
    return results


# ==================================
# Expériences â€” aides de nettoyage
# ==================================
def _clean_experiences(exps: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Retire quelques faux positifs (entreprise = date/type de contrat) et doublons.
    """
    cleaned: List[Dict[str, str]] = []
    seen = set()
    for exp in exps:
        company = (exp.get("entreprise") or "").strip()

        # Exclusions typiques (mois FR + année dans entreprise)
        if re.search(r"(janv\.|févr\.|mars|avr\.|mai|juin|juil\.|aout|sept\.|oct\.|nov\.|déc\.)\s+\d{4}", company, re.I):
            continue
        if CONTRACT_REGEX.search(company):
            continue

        key = (
            (exp.get("titre") or "").strip(),
            company,
            (exp.get("dates") or "").strip(),
            (exp.get("lieu") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(exp)
    return cleaned


# ==================================
# Expériences â€” extracteurs
# ==================================
async def extract_experiences_without_click(page: Page, html_content: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Extrait les expériences visibles (sans clic â€œvoir plusâ€).
    Retourne toujours une liste d'objets {titre, entreprise, type contrat, dates, lieu}
    """
    # html_content est ignoré dans cette version (Playwright direct) â€” conservé pour compat
    results: List[Dict[str, str]] = []
    try:
        loc = await _locale_hint(page)
        await _scroll_to_section(page, "experience", loc, timeout=3000)

        rows = await _extract_cards(page, loc, section_key="experience")

        # Heuristique mapping : title=poste, subtitle=entreprise, meta=dates, description=lieu/détails
        for r in rows:
            title = r.get("title", "")
            subtitle = r.get("subtitle", "")
            meta = r.get("meta", "")
            descr = r.get("description", "")

            # Essai d'extraire un type de contrat depuis subtitle
            contract_type = ""
            m = CONTRACT_REGEX.search(subtitle)
            if m:
                contract_type = m.group(0)
                # Retirer le contrat de l'entreprise si collé "Entreprise Â· CDI"
                subtitle = re.sub(rf"\s*Â·\s*{re.escape(contract_type)}\s*$", "", subtitle, flags=re.I)

            results.append({
                "titre": title,
                "entreprise": subtitle,
                "type contrat": contract_type,
                "dates": meta,
                "lieu": descr,
            })

        return _clean_experiences(results)

    except Exception:
        return []


async def extract_experiences_with_click(page: Page, html_content: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Extrait les expériences après clic â€œVoir toutes les expériencesâ€.
    Retourne toujours une liste d'objets {titre, entreprise, type contrat, dates, lieu}
    """
    # html_content est ignoré (Playwright direct) â€” conservé pour compat
    results: List[Dict[str, str]] = []
    try:
        loc = await _locale_hint(page)

        # La page â€œvoir toutes les expériencesâ€ a souvent une structure différente ;
        # on réutilise le mÃªme extracteur générique en changeant si besoin containers_key
        rows = await _extract_cards(page, loc, section_key="experience")

        for r in rows:
            title = r.get("title", "")
            subtitle = r.get("subtitle", "")
            meta = r.get("meta", "")
            descr = r.get("description", "")

            contract_type = ""
            m = CONTRACT_REGEX.search(subtitle)
            if m:
                contract_type = m.group(0)
                subtitle = re.sub(rf"\s*Â·\s*{re.escape(contract_type)}\s*$", "", subtitle, flags=re.I)

            results.append({
                "titre": title,
                "entreprise": subtitle,
                "type contrat": contract_type,
                "dates": meta,
                "lieu": descr,
            })

        return _clean_experiences(results)

    except Exception:
        return []



