"""Relógio da aplicação.

Convenção do banco (todas as colunas são `DateTime` sem fuso):
- Horários de agenda (`Agendamento.data_hora`, horário de funcionamento, horário de
  resposta do bot) são horário de parede **local da empresa** — é o que o cliente
  digita e lê no WhatsApp. Compare-os sempre com `agora_local(empresa)`.
- Timestamps de auditoria (`criado_em`, `cancelado_em`, `*_enviado_em`, expiração
  de token...) são **UTC**. Use `agora_utc()`.
"""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

FUSO_PADRAO = "America/Sao_Paulo"

# O Brasil não tem horário de verão desde 2019: estes quatro cobrem todo o país.
FUSOS_DISPONIVEIS = {
    "America/Noronha": "Fernando de Noronha (UTC−2)",
    "America/Sao_Paulo": "Brasília (UTC−3)",
    "America/Manaus": "Amazonas, MT, MS, RO e RR (UTC−4)",
    "America/Rio_Branco": "Acre (UTC−5)",
}


def agora_utc() -> datetime:
    """Instante atual em UTC, sem tzinfo (formato das colunas de auditoria)."""
    return datetime.now(UTC).replace(tzinfo=None)


def fuso_da_empresa(empresa) -> ZoneInfo:
    nome = getattr(empresa, "fuso_horario", None) or FUSO_PADRAO
    try:
        return ZoneInfo(nome)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Fuso horário inválido '%s' na empresa %s; usando %s", nome, getattr(empresa, "id", None), FUSO_PADRAO)
        return ZoneInfo(FUSO_PADRAO)


def agora_local(empresa=None, agora: datetime | None = None) -> datetime:
    """Horário de parede atual no fuso da empresa (ou no fuso padrão), sem tzinfo.

    `agora` é um instante UTC sem tzinfo — permite converter um instante já fixado
    (ex.: um ciclo de lembretes inteiro usando o mesmo relógio).
    """
    instante = (agora or agora_utc()).replace(tzinfo=UTC)
    return instante.astimezone(fuso_da_empresa(empresa)).replace(tzinfo=None)
