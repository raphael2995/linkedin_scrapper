# utils_scripts/housekeeping.py
# -*- coding: utf-8 -*-
"""
Utilitaires de ménage et de résumé d'outputs :
- purge_old_files : supprime les vieux artefacts (CSV/JSONL) avec garde-fous
- summarize_outputs : imprime un résumé lisible des derniers fichiers produits
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
from typing import Iterable, Tuple
from linkedin_scrapper.logging_setup import get_logger
logger = get_logger(__name__)

def purge_old_files(
    dirs: Iterable[Path],
    glob_patterns: Tuple[str, ...] = ("*.csv", "*.jsonl"),
    older_than_days: int = 7,
    keep_at_least: int = 5,
) -> None:
    """
    Supprime les fichiers plus vieux que `older_than_days` dans `dirs`,
    en conservant au minimum `keep_at_least` fichiers les plus récents par dossier/pattern.
    """
    cutoff = datetime.now() - timedelta(days=older_than_days)

    for base in dirs:
        base = Path(base)
        if not base.exists():
            continue

        for pattern in glob_patterns:
            files = sorted(
                base.glob(pattern),
                key=lambda p: (p.stat().st_mtime, p.name),
                reverse=True,
            )
            if not files:
                continue

            # garde au moins N fichiers récents
            #survivors = files[:keep_at_least]
            candidates = files[keep_at_least:]

            removed = 0
            for f in candidates:
                try:
                    if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                        f.unlink(missing_ok=True)
                        removed += 1
                except Exception:
                    # on ne casse pas le pipeline pour du ménage
                    pass

            if removed:
                logger.info("[cleanup] %s â€¢ pattern '%s' → %d fichier(s) supprimé(s)", base, pattern, removed)


def _fmt_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def summarize_outputs(outputs_dir: Path, profiles_dir: Path) -> None:
    """
    Affiche un petit état des lieux :
    - derniers fichiers step1 (pipeline_search_results_*.csv)
    - derniers JSONL/CSV de profils (step2)
    - derniers reportings (step3)
    """
    outputs_dir = Path(outputs_dir)
    profiles_dir = Path(profiles_dir)

    def last_n(glob_pat: str, where: Path, n: int = 3):
        files = sorted(where.glob(glob_pat), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
        lines = []
        for f in files:
            ts = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  - {f.name}  ({_fmt_size(f.stat().st_size)} ; {ts})")
        return lines

    logger.info("\n===== Résumé des sorties =====")

    # Step1
    logger.info("[Step1] Derniers résultats de recherche :")
    step1 = last_n("pipeline_search_results_*.csv", outputs_dir)
    logger.info("\n".join(step1) if step1 else "  (aucun)")

    # Step2
    logger.info("\n[Step2] Derniers profils scrapés :")
    step2_jsonl = last_n("profiles_*.jsonl", profiles_dir)
    step2_csv   = last_n("profiles_*.csv", profiles_dir)
    if step2_jsonl or step2_csv:
        logger.info("\n".join(step2_jsonl + step2_csv))
    else:
        logger.info("  (aucun)")

    # Step3
    logger.info("\n[Step3] Derniers reportings :")
    rep_dir = outputs_dir / "reporting"
    step3 = last_n("reporting_*.csv", rep_dir) if rep_dir.exists() else []
    logger.info("\n".join(step3) if step3 else "  (aucun)")
    logger.info("===============================\n")



