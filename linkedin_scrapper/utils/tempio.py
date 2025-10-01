"""
Utilitaires de temporaires éphémères (mémoire ou OS).
"""
from contextlib import contextmanager
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Iterator

@contextmanager
def temp_file(suffix: str = "", prefix: str = "tmp_", delete: bool = True) -> Iterator[Path]:
    """Crée un fichier temporaire auto-supprimé en sortie (delete=True)."""
    with NamedTemporaryFile(suffix=suffix, prefix=prefix, delete=delete) as f:
        yield Path(f.name)

@contextmanager
def temp_dir(prefix: str = "tmpdir_") -> Iterator[Path]:
    """Crée un répertoire temporaire supprimé automatiquement en sortie."""
    with TemporaryDirectory(prefix=prefix) as d:
        yield Path(d)

def mem_text() -> StringIO:
    """Buffer texte (CSV/JSON) en mémoire."""
    return StringIO()

def mem_bytes() -> BytesIO:
    """Buffer binaire (Excel, téléchargements) en mémoire."""
    return BytesIO()



