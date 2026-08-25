from __future__ import annotations

import logging

from .redis_client import redis_cliente

logger = logging.getLogger(__name__)


def excedeu_limite(chave: str, limite: int, janela_segundos: int) -> bool:
    """True se a chave já bateu o limite de tentativas dentro da janela.

    Contador simples em Redis (INCR + EXPIRE na primeira ocorrência). Nunca
    levanta exceção — se o Redis estiver indisponível, deixa passar: rate
    limit é defesa em profundidade, não deve derrubar login/cadastro por causa
    disso (mesmo princípio de `services.configuracoes.obter_configuracao_isolada`).
    """
    try:
        contagem = redis_cliente.incr(chave)
        if contagem == 1:
            redis_cliente.expire(chave, janela_segundos)
        return contagem > limite
    except Exception:
        logger.warning("Rate limit indisponível (Redis) para a chave %s — deixando passar.", chave)
        return False


def ip_do_cliente(request) -> str:
    return request.client.host if request.client else "desconhecido"
