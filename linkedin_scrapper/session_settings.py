# session_settings.py
from pathlib import Path
import atexit
from tempfile import TemporaryDirectory
import os
from datetime import datetime

from linkedin_scrapper.config import settings

"""
Centralisation des chemins & paramètres runtime.

- Les RESSOURCES packagées (config, storage, etc.) restent dans le package.
- Les DONNÉES (inputs/outputs) sont unifiées dans ./data/ à la racine du repo.
- Tu peux surcharger l’emplacement des données via l’ENV LINKSCRAPER_DATA_DIR.
"""

# ======================
# Racines & dossiers
# ======================
PKG_ROOT = Path(__file__).resolve().parent          # .../linkedin_scrapper
REPO_ROOT = PKG_ROOT.parent                         # .../ (racine projet)

# Ressources PACKAGÉES (ne bougent pas)
CONFIG_DIR = PKG_ROOT / "config"

# Données (entrées/sorties) — UNIFIÉES à la racine
DATA_DIR = Path(os.environ.get("LINKSCRAPER_DATA_DIR", REPO_ROOT / "data")).resolve()
OUTPUTS_DIR = DATA_DIR / "outputs"
INPUTS_DIR = DATA_DIR / "inputs"

# Temporaire éphémère (auto-supprimé en fin de process)
_tmpdir = TemporaryDirectory(prefix="linkedin_scrapper_")
TEMP_DIR = Path(_tmpdir.name)
atexit.register(_tmpdir.cleanup)

# Dossiers de sortie (clairs & stables)
STEP1_DIR = OUTPUTS_DIR / "step1"             # résultats finaux Étape 1
PROFILES_DIR = OUTPUTS_DIR / "step2_profiles" # JSONL + CSV miroir Étape 2
REPORTING_DIR = OUTPUTS_DIR / "reporting"     # CSV de reporting Étape 3

# Dossiers temporaires
TEMP_STEP1_DIR = TEMP_DIR / "step1"           # intermédiaires Étape 1

# Création de l'arborescence
for d in (
    CONFIG_DIR, DATA_DIR, OUTPUTS_DIR, INPUTS_DIR,
    STEP1_DIR, PROFILES_DIR, REPORTING_DIR, TEMP_STEP1_DIR
):
    d.mkdir(parents=True, exist_ok=True)

# ======================
# Session LinkedIn
# ======================
# Le storage (cookies/session) RESTE dans le package par défaut
STORAGE_STATE_PATH = CONFIG_DIR / "linkedin_storage.json"
STORAGE_STATE = os.environ.get("LINKEDIN_STORAGE_STATE", str(STORAGE_STATE_PATH))

USER_AGENT = os.environ.get(
    "LINKEDIN_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

def platform_value_from_user_agent(ua: str) -> str:
    """Déduit navigator.platform à partir du User-Agent."""
    return "Win32" if ("Windows" in ua or "Win64" in ua) else "MacIntel"

# ======================
# Affichage navigateur
# ======================
HEADLESS = os.environ.get("HEADLESS", "false").lower() in {"1", "true", "yes"}

# ======================
# Timeouts & pacing (millisecondes)
# ======================
def _to_int(env_name: str, default: int) -> int:
    try:
        return int(os.environ.get(env_name, default))
    except Exception:
        return default

# Playwright (timeout de navigation par défaut)
DEFAULT_TIMEOUT_MS = settings.navigation_timeout_ms

# Frappe “humaine”
HUMAN_MIN_DELAY_MS = _to_int("LINKEDIN_HUMAN_MIN_DELAY_MS", 80)
HUMAN_MAX_DELAY_MS = _to_int("LINKEDIN_HUMAN_MAX_DELAY_MS", 200)

# Think-time avant chaque recherche
THINK_MIN_MS = _to_int("LINKEDIN_THINK_MIN_MS", 2_000)
THINK_MAX_MS = _to_int("LINKEDIN_THINK_MAX_MS", 4_000)

# Micro-pause + jitter entre requêtes
MICRO_PAUSE_MS = _to_int("LINKEDIN_MICRO_PAUSE_MS", 1_500)
JITTER_MIN_MS = _to_int("LINKEDIN_JITTER_MIN_MS", 600)
JITTER_MAX_MS = _to_int("LINKEDIN_JITTER_MAX_MS", 1_800)

# Cooldown périodique
LONG_COOLDOWN_EVERY = _to_int("LINKEDIN_LONG_COOLDOWN_EVERY", 10)
LONG_COOLDOWN_MIN_MS = _to_int("LINKEDIN_LONG_COOLDOWN_MIN_MS", 45_000)
LONG_COOLDOWN_MAX_MS = _to_int("LINKEDIN_LONG_COOLDOWN_MAX_MS", 90_000)

# Backoff après échec
BACKOFF_MIN_MS = _to_int("LINKEDIN_BACKOFF_MIN_MS", 10_000)
BACKOFF_MAX_MS = _to_int("LINKEDIN_BACKOFF_MAX_MS", 20_000)

# Pause après chargement des résultats
SETTLE_MIN_MS = _to_int("LINKEDIN_SETTLE_MIN_MS", 700)
SETTLE_MAX_MS = _to_int("LINKEDIN_SETTLE_MAX_MS", 1_500)

# ======================
# Horodatage “propre”
# ======================
def timestamp_min() -> str:
    """Horodatage sans secondes : YYYYMMDD_HHMM."""
    return datetime.now().strftime("%Y%m%d_%H%M")
