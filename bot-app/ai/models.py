from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    AGENDAR = "agendar"
    CANCELAR = "cancelar"
    REAGENDAR = "reagendar"
    CONSULTAR_HORARIOS = "consultar_horarios"
    CONSULTAR_SERVICOS = "consultar_servicos"
    CONSULTAR_PRECOS = "consultar_precos"
    FALAR_COM_ATENDENTE = "falar_com_atendente"
    SAUDACAO = "saudacao"
    DESCONHECIDO = "desconhecido"


@dataclass
class Entidades:
    servico: str | None = None
    data: str | None = None
    horario: str | None = None
    periodo: str | None = None
    nome: str | None = None
    telefone: str | None = None


@dataclass
class InterpretacaoIA:
    intent: Intent
    entidades: Entidades = field(default_factory=Entidades)
    confianca: float = 0.0
    origem: str = "fallback"  # "ia" | "cache" | "fallback"
