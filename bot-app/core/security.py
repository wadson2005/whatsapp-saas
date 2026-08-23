from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGORITMO = "sha256"
_ITERACOES = 260_000


def hash_senha(senha: str) -> str:
    salt = secrets.token_hex(16)
    derivado = hashlib.pbkdf2_hmac(_ALGORITMO, senha.encode("utf-8"), bytes.fromhex(salt), _ITERACOES)
    return f"{salt}${derivado.hex()}"


def verificar_senha(senha: str, hash_armazenado: str) -> bool:
    try:
        salt, derivado_hex = hash_armazenado.split("$", 1)
    except ValueError:
        return False
    calculado = hashlib.pbkdf2_hmac(_ALGORITMO, senha.encode("utf-8"), bytes.fromhex(salt), _ITERACOES)
    return hmac.compare_digest(calculado.hex(), derivado_hex)
