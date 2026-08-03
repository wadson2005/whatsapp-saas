import asyncio
import importlib
import json
import sys
from types import SimpleNamespace

from conftest import BOOTSTRAP_ENV as _SHARED_ENV
from conftest import PROJECT_ROOT, FakeRedis

BOOTSTRAP_ENV = {**_SHARED_ENV, "DATABASE_URL": "sqlite:///:memory:"}


class FakeAIProvider:
    def __init__(self, respostas=None, excecao=None, atraso_segundos: float = 0.0):
        self.respostas = list(respostas or [])
        self.excecao = excecao
        self.atraso_segundos = atraso_segundos
        self.chamadas = 0

    async def completar(self, mensagens):
        self.chamadas += 1
        if self.atraso_segundos:
            await asyncio.sleep(self.atraso_segundos)
        if self.excecao:
            raise self.excecao
        return self.respostas.pop(0)


def carregar_ai(monkeypatch):
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    for chave, valor in BOOTSTRAP_ENV.items():
        monkeypatch.setenv(chave, valor)

    for modulo in ["core.config", "core.redis_client", "ai.cache", "ai.models", "ai.provider", "ai.prompts", "ai.service", "ai"]:
        sys.modules.pop(modulo, None)

    cache = importlib.import_module("ai.cache")
    models = importlib.import_module("ai.models")
    service = importlib.import_module("ai.service")
    cache.redis_cliente = FakeRedis()
    return cache, models, service


def _resposta_json(intent: str, **entidades) -> str:
    return json.dumps({"intent": intent, "entidades": entidades, "confianca": 0.9})


def test_interpretacao_correta_retorna_intent_e_entidades(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    provider = FakeAIProvider(respostas=[_resposta_json("cancelar", servico="Corte")])
    ai_service = service.AIService(provider=provider, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=True)

    resultado = asyncio.run(ai_service.interpretar(1, "não vou poder ir no meu horário"))

    assert resultado.intent == models.Intent.CANCELAR
    assert resultado.entidades.servico == "Corte"
    assert resultado.origem == "ia"
    assert provider.chamadas == 1


def test_timeout_retorna_fallback(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    provider = FakeAIProvider(respostas=[_resposta_json("cancelar")], atraso_segundos=0.2)
    ai_service = service.AIService(provider=provider, timeout_segundos=0.01, cache_ttl_segundos=60, habilitado=True)

    resultado = asyncio.run(ai_service.interpretar(1, "mensagem qualquer"))

    assert resultado.intent == models.Intent.DESCONHECIDO
    assert resultado.origem == "fallback"


def test_erro_do_provider_retorna_fallback(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    provider = FakeAIProvider(excecao=service.AIProviderError("falha simulada"))
    ai_service = service.AIService(provider=provider, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=True)

    resultado = asyncio.run(ai_service.interpretar(1, "mensagem qualquer"))

    assert resultado.intent == models.Intent.DESCONHECIDO
    assert resultado.origem == "fallback"


def test_resposta_invalida_do_provider_retorna_fallback(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    provider = FakeAIProvider(respostas=["isso não é json"])
    ai_service = service.AIService(provider=provider, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=True)

    resultado = asyncio.run(ai_service.interpretar(1, "mensagem qualquer"))

    assert resultado.intent == models.Intent.DESCONHECIDO
    assert resultado.origem == "fallback"


def test_cache_evita_segunda_chamada_ao_provider(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    provider = FakeAIProvider(respostas=[_resposta_json("reagendar")])
    ai_service = service.AIService(provider=provider, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=True)

    primeira = asyncio.run(ai_service.interpretar(1, "posso trocar pra sexta?"))
    segunda = asyncio.run(ai_service.interpretar(1, "posso trocar pra sexta?"))

    assert provider.chamadas == 1
    assert primeira.origem == "ia"
    assert segunda.origem == "cache"
    assert segunda.intent == models.Intent.REAGENDAR


def test_cache_isola_por_empresa(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    provider = FakeAIProvider(respostas=[_resposta_json("reagendar"), _resposta_json("cancelar")])
    ai_service = service.AIService(provider=provider, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=True)

    resultado_empresa_1 = asyncio.run(ai_service.interpretar(1, "mesmo texto"))
    resultado_empresa_2 = asyncio.run(ai_service.interpretar(2, "mesmo texto"))

    assert provider.chamadas == 2
    assert resultado_empresa_1.intent == models.Intent.REAGENDAR
    assert resultado_empresa_2.intent == models.Intent.CANCELAR


def test_ia_desabilitada_retorna_fallback_sem_chamar_provider(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    provider = FakeAIProvider(respostas=[_resposta_json("cancelar")])
    ai_service = service.AIService(provider=provider, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=False)

    resultado = asyncio.run(ai_service.interpretar(1, "mensagem qualquer"))

    assert resultado.intent == models.Intent.DESCONHECIDO
    assert resultado.origem == "fallback"
    assert provider.chamadas == 0


def test_provider_none_retorna_fallback(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    ai_service = service.AIService(provider=None, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=True)

    resultado = asyncio.run(ai_service.interpretar(1, "mensagem qualquer"))

    assert resultado.intent == models.Intent.DESCONHECIDO
    assert resultado.origem == "fallback"


def test_provider_nao_suportado_desativa_ia_sem_lancar_excecao(monkeypatch):
    _, models, service = carregar_ai(monkeypatch)

    config = SimpleNamespace(
        ai_enabled=True,
        ai_api_key="chave-qualquer",
        ai_provider="provider-inexistente",
        ai_model="gpt-4o-mini",
        ai_timeout_segundos=2.0,
        ai_cache_ttl_segundos=60,
    )

    ai_service = service.criar_ai_service(config)
    resultado = asyncio.run(ai_service.interpretar(1, "mensagem qualquer"))

    assert resultado.intent == models.Intent.DESCONHECIDO
    assert resultado.origem == "fallback"


def test_falha_no_redis_durante_cache_nao_propaga_excecao(monkeypatch):
    cache, models, service = carregar_ai(monkeypatch)

    class RedisComFalha:
        def get(self, key):
            raise ConnectionError("redis indisponível")

        def set(self, key, value, ex=None):
            raise ConnectionError("redis indisponível")

    cache.redis_cliente = RedisComFalha()

    provider = FakeAIProvider(respostas=[_resposta_json("cancelar")])
    ai_service = service.AIService(provider=provider, timeout_segundos=2.0, cache_ttl_segundos=60, habilitado=True)

    resultado = asyncio.run(ai_service.interpretar(1, "mensagem qualquer"))

    assert resultado.intent == models.Intent.DESCONHECIDO
    assert resultado.origem == "fallback"
