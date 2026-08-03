from __future__ import annotations

import asyncio
import json
import logging

from . import cache, prompts
from .models import Entidades, InterpretacaoIA, Intent
from .provider import AIProvider, AIProviderError

logger = logging.getLogger(__name__)


class AIService:
    """Camada única de interpretação de linguagem natural.

    Desacopla o restante do sistema do provedor de IA: quem chama só conhece
    `interpretar()` e o resultado tipado `InterpretacaoIA`. Qualquer falha
    (timeout, erro do provedor, JSON inválido) vira um resultado "desconhecido"
    em vez de propagar exceção — o chamador nunca precisa tratar erro de IA.
    """

    def __init__(
        self,
        provider: AIProvider | None,
        timeout_segundos: float,
        cache_ttl_segundos: int,
        habilitado: bool,
    ):
        self._provider = provider
        self._timeout_segundos = timeout_segundos
        self._cache_ttl_segundos = cache_ttl_segundos
        self._habilitado = habilitado and provider is not None

    async def interpretar(self, empresa_id: int, texto: str, contexto_empresa: str | None = None) -> InterpretacaoIA:
        if not self._habilitado:
            return _resultado_fallback()

        try:
            cache_hit = cache.obter(empresa_id, texto)
            if cache_hit is not None:
                return _from_dict(cache_hit, origem="cache")

            mensagens = prompts.montar_mensagens(texto, contexto_empresa)
            bruto = await asyncio.wait_for(self._provider.completar(mensagens), timeout=self._timeout_segundos)
            dado = json.loads(bruto)
            resultado = _from_dict(dado, origem="ia")
            cache.salvar(empresa_id, texto, dado, self._cache_ttl_segundos)
        except (TimeoutError, AIProviderError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Falha ao interpretar mensagem via IA (%s): %s", type(exc).__name__, exc)
            return _resultado_fallback()
        except Exception:
            logger.exception("Erro inesperado no AIService")
            return _resultado_fallback()

        return resultado


def _from_dict(dado: dict, origem: str) -> InterpretacaoIA:
    try:
        intent = Intent(dado.get("intent"))
    except ValueError:
        intent = Intent.DESCONHECIDO

    entidades_dado = dado.get("entidades") or {}
    entidades = Entidades(
        servico=entidades_dado.get("servico"),
        data=entidades_dado.get("data"),
        horario=entidades_dado.get("horario"),
        periodo=entidades_dado.get("periodo"),
        nome=entidades_dado.get("nome"),
        telefone=entidades_dado.get("telefone"),
    )

    try:
        confianca = float(dado.get("confianca") or 0)
    except (TypeError, ValueError):
        confianca = 0.0

    return InterpretacaoIA(intent=intent, entidades=entidades, confianca=confianca, origem=origem)


def _resultado_fallback() -> InterpretacaoIA:
    return InterpretacaoIA(intent=Intent.DESCONHECIDO, entidades=Entidades(), confianca=0.0, origem="fallback")


def criar_ai_service() -> AIService:
    from config import settings

    if not settings.ai_enabled or not settings.ai_api_key:
        return AIService(provider=None, timeout_segundos=settings.ai_timeout_segundos, cache_ttl_segundos=settings.ai_cache_ttl_segundos, habilitado=False)

    if settings.ai_provider != "openai":
        logger.error(
            "AI_PROVIDER=%r não é suportado (apenas 'openai' está implementado); camada de IA desativada.",
            settings.ai_provider,
        )
        return AIService(provider=None, timeout_segundos=settings.ai_timeout_segundos, cache_ttl_segundos=settings.ai_cache_ttl_segundos, habilitado=False)

    from .provider import OpenAIProvider

    provider = OpenAIProvider(
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        timeout_segundos=settings.ai_timeout_segundos,
    )

    return AIService(
        provider=provider,
        timeout_segundos=settings.ai_timeout_segundos,
        cache_ttl_segundos=settings.ai_cache_ttl_segundos,
        habilitado=True,
    )


ai_service = criar_ai_service()
