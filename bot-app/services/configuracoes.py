from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

from core.config import settings
from core.database import SessionLocal
from core.models import ConfiguracaoSistema

logger = logging.getLogger(__name__)

CONFIGURACAO_ID = 1

_CAMPOS_EDITAVEIS = (
    "meta_token",
    "meta_phone_number_id",
    "meta_business_id",
    "bot_activation_words_raw",
    "meta_template_lembrete_nome",
    "meta_template_lembrete_idioma",
    "lembrete_antecedencia_horas",
    "lembrete_intervalo_minutos",
    "ai_enabled",
    "ai_provider",
    "ai_api_key",
    "ai_model",
    "ai_timeout_segundos",
    "ai_cache_ttl_segundos",
)


def _valores_padrao_do_env() -> dict:
    return {
        "meta_token": settings.meta_token,
        "meta_phone_number_id": settings.meta_phone_number_id,
        "meta_business_id": settings.meta_business_id,
        "bot_activation_words_raw": settings.bot_activation_words_raw,
        "meta_template_lembrete_nome": settings.meta_template_lembrete_nome,
        "meta_template_lembrete_idioma": settings.meta_template_lembrete_idioma,
        "lembrete_antecedencia_horas": settings.lembrete_antecedencia_horas,
        "lembrete_intervalo_minutos": settings.lembrete_intervalo_minutos,
        "ai_enabled": settings.ai_enabled,
        "ai_provider": settings.ai_provider,
        "ai_api_key": settings.ai_api_key,
        "ai_model": settings.ai_model,
        "ai_timeout_segundos": settings.ai_timeout_segundos,
        "ai_cache_ttl_segundos": settings.ai_cache_ttl_segundos,
    }


def obter_configuracao(db) -> ConfiguracaoSistema:
    """Busca a linha única de configuração, criando com os valores do .env se ainda não existir."""
    config = db.query(ConfiguracaoSistema).filter_by(id=CONFIGURACAO_ID).first()
    if not config:
        config = ConfiguracaoSistema(id=CONFIGURACAO_ID, **_valores_padrao_do_env())
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def atualizar_configuracao(db, **campos) -> ConfiguracaoSistema:
    config = obter_configuracao(db)
    for campo, valor in campos.items():
        if campo not in _CAMPOS_EDITAVEIS:
            raise ValueError(f"Campo de configuração desconhecido: {campo}")
        setattr(config, campo, valor)
    config.atualizado_em = datetime.utcnow()
    db.commit()
    db.refresh(config)
    return config


def _snapshot_do_env() -> SimpleNamespace:
    return SimpleNamespace(**_valores_padrao_do_env())


def obter_configuracao_isolada() -> SimpleNamespace:
    """Versão sem sessão externa — para módulos sem uma sessão de request em mãos (meta_client.py).

    Abre e fecha a própria sessão a cada chamada (custo desprezível: um SELECT por
    chave primária). Se o banco estiver inacessível, cai para os valores do .env em
    vez de propagar exceção — configuração indisponível nunca deve derrubar o envio
    de mensagem.
    """
    try:
        db = SessionLocal()
    except Exception:
        logger.exception("Falha ao abrir conexão para ler configuração; usando valores do .env")
        return _snapshot_do_env()

    try:
        config = obter_configuracao(db)
        return SimpleNamespace(**{campo: getattr(config, campo) for campo in _CAMPOS_EDITAVEIS})
    except Exception:
        logger.exception("Falha ao ler configuração do banco; usando valores do .env")
        return _snapshot_do_env()
    finally:
        db.close()


def parse_activation_words(raw: str | None) -> tuple[str, ...]:
    palavras = [palavra.strip().lower() for palavra in (raw or "").split(",") if palavra.strip()]
    return tuple(palavras) if palavras else ("oibot",)
