"""
Paramétrage centralisé de l'application.
Charge .env s'il existe, avec des valeurs par défaut sures.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Dossiers locaux (jamais versionnés)
    cookies_dir: str = ".local_state"
    session_dir: str = ".local_state"

    # Paramètres d'exécution
    navigation_timeout_ms: int = 15000
    max_retries: int = 3
    google_search_delay_ms: int = 800

    # I/O
    input_csv: str = "data/input.csv"
    output_csv: str = "data/out/profiles.csv"
    output_xlsx: str = "data/out/profiles.xlsx"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()



