from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import joinedload

from core.models import Agendamento
from integrations.meta_client import enviar_template

from .agenda import formatar_data_hora
from .configuracoes import obter_configuracao

logger = logging.getLogger(__name__)

STATUS_ELEGIVEIS_PARA_LEMBRETE = ("agendado", "confirmado")


def buscar_agendamentos_para_lembrete(db, agora: datetime | None = None, config=None) -> list[Agendamento]:
    agora = agora or datetime.utcnow()
    config = config or obter_configuracao(db)
    limite = agora + timedelta(hours=config.lembrete_antecedencia_horas)

    return (
        db.query(Agendamento)
        .options(
            joinedload(Agendamento.cliente_final),
            joinedload(Agendamento.servico),
            joinedload(Agendamento.empresa),
        )
        .filter(
            Agendamento.status.in_(STATUS_ELEGIVEIS_PARA_LEMBRETE),
            Agendamento.lembrete_enviado_em.is_(None),
            Agendamento.data_hora > agora,
            Agendamento.data_hora <= limite,
        )
        .all()
    )


async def enviar_lembrete(db, agendamento: Agendamento, config=None) -> bool:
    config = config or obter_configuracao(db)
    cliente = agendamento.cliente_final
    servico = agendamento.servico
    empresa = agendamento.empresa

    parametros_corpo = [
        cliente.nome or "cliente",
        servico.nome,
        formatar_data_hora(agendamento.data_hora),
        empresa.nome,
    ]

    try:
        resultado = await enviar_template(
            numero=cliente.telefone,
            nome_template=config.meta_template_lembrete_nome,
            idioma=config.meta_template_lembrete_idioma,
            parametros_corpo=parametros_corpo,
        )
    except Exception:
        logger.exception("Falha ao enviar lembrete do agendamento %s", agendamento.id)
        return False

    if resultado.get("error"):
        logger.error(
            "Meta rejeitou o lembrete do agendamento %s: %s",
            agendamento.id,
            resultado["error"],
        )
        return False

    agendamento.lembrete_enviado_em = datetime.utcnow()
    db.commit()
    return True


async def enviar_lembretes_pendentes(db) -> int:
    config = obter_configuracao(db)
    agendamentos = buscar_agendamentos_para_lembrete(db, config=config)
    enviados = 0
    for agendamento in agendamentos:
        if await enviar_lembrete(db, agendamento, config=config):
            enviados += 1
    return enviados
