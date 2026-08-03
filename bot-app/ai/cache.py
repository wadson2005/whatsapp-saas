from __future__ import annotations

import hashlib
import json

from core.redis_client import redis_cliente

PREFIXO_CACHE = "ai:cache"


def _chave(empresa_id: int, texto: str) -> str:
    texto_normalizado = texto.strip().lower()
    hash_texto = hashlib.sha256(texto_normalizado.encode("utf-8")).hexdigest()
    return f"{PREFIXO_CACHE}:{empresa_id}:{hash_texto}"


def obter(empresa_id: int, texto: str) -> dict | None:
    bruto = redis_cliente.get(_chave(empresa_id, texto))
    if not bruto:
        return None
    return json.loads(bruto)


def salvar(empresa_id: int, texto: str, resultado: dict, ttl_segundos: int) -> None:
    redis_cliente.set(_chave(empresa_id, texto), json.dumps(resultado), ex=ttl_segundos)
