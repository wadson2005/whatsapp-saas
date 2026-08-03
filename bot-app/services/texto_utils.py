from __future__ import annotations

import re
import unicodedata


def normalizar_texto(texto: str) -> str:
    bruto = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(ch for ch in bruto if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", sem_acentos.lower()).strip()
