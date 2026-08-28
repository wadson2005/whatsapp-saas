from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import joinedload

from core.models import Agendamento, Empresa
from integrations.email_client import EmailError, enviar_email

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
        .join(Empresa, Agendamento.empresa_id == Empresa.id)
        .options(
            joinedload(Agendamento.cliente_final),
            joinedload(Agendamento.servico),
            joinedload(Agendamento.empresa),
        )
        .filter(
            Agendamento.status.in_(STATUS_ELEGIVEIS_PARA_LEMBRETE),
            Agendamento.data_hora > agora,
            Agendamento.data_hora <= limite,
            Empresa.lembrete_canal_email.is_(True),
            Agendamento.lembrete_email_enviado_em.is_(None),
        )
        .all()
    )


async def _enviar_lembrete_email(db, agendamento: Agendamento) -> bool:
    cliente = agendamento.cliente_final
    servico = agendamento.servico
    empresa = agendamento.empresa

    corpo = (
        f"Olá {cliente.nome or 'cliente'}!\n\n"
        f"Passando para lembrar do seu horário de {servico.nome} marcado para "
        f"{formatar_data_hora(agendamento.data_hora)} na {empresa.nome}.\n\n"
        "Se precisar cancelar ou remarcar, é só responder a mensagem do WhatsApp."
    )
    assunto = f"Lembrete: {servico.nome} em {formatar_data_hora(agendamento.data_hora)}"

    try:
        await enviar_email(cliente.email, assunto, corpo)
    except EmailError:
        logger.exception("Falha ao enviar lembrete por e-mail do agendamento %s", agendamento.id)
        return False

    agendamento.lembrete_email_enviado_em = datetime.utcnow()
    return True


async def enviar_lembrete(db, agendamento: Agendamento) -> bool:
    """Envia o lembrete por e-mail para a empresa que tiver o canal ativado.

    Único canal disponível no momento (o lembrete por WhatsApp foi removido por
    depender de aprovação de template no Meta Business Manager). Se o cliente
    não tiver e-mail cadastrado, fecha o ciclo sem tentar de novo a cada rodada
    do loop de lembretes, em vez de ficar reprocessando o mesmo agendamento.
    """
    empresa = agendamento.empresa
    cliente = agendamento.cliente_final

    if not empresa.lembrete_canal_email or agendamento.lembrete_email_enviado_em is not None:
        return False

    if not cliente.email:
        agendamento.lembrete_email_enviado_em = datetime.utcnow()
        db.commit()
        return False

    enviou = await _enviar_lembrete_email(db, agendamento)
    db.commit()
    return enviou


async def enviar_lembretes_pendentes(db) -> int:
    config = obter_configuracao(db)
    agendamentos = buscar_agendamentos_para_lembrete(db, config=config)
    enviados = 0
    for agendamento in agendamentos:
        if await enviar_lembrete(db, agendamento):
            enviados += 1
    return enviados
