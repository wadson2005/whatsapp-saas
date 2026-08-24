from __future__ import annotations

import re
import unicodedata


def normalizar_texto(texto: str) -> str:
    bruto = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(ch for ch in bruto if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", sem_acentos.lower()).strip()


def gerar_slug(texto: str) -> str:
    """Converte um texto livre (com acentos, espaços, símbolos) num slug válido.

    Usado para sugerir automaticamente o identificador da empresa a partir do
    nome digitado — o slug em si continua restrito a `a-z0-9-` (é usado como
    nome de instância na Evolution API e em URLs, não pode ter acento/símbolo).
    """
    normalizado = normalizar_texto(texto)
    com_hifens = re.sub(r"[^a-z0-9]+", "-", normalizado)
    return com_hifens.strip("-")
