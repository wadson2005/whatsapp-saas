from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import joinedload

from core.models import Agendamento, Empresa
from integrations.email_client import EmailError, enviar_email
from integrations.meta_client import enviar_template

from .agenda import formatar_data_hora
from .configuracoes import (
    limpar_erro_lembrete_whatsapp,
    obter_configuracao,
    registrar_erro_lembrete_whatsapp,
)

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
            or_(
                and_(Empresa.lembrete_canal_whatsapp.is_(True), Agendamento.lembrete_enviado_em.is_(None)),
                and_(Empresa.lembrete_canal_email.is_(True), Agendamento.lembrete_email_enviado_em.is_(None)),
            ),
        )
        .all()
    )


async def _enviar_lembrete_whatsapp(db, agendamento: Agendamento, config) -> bool:
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
        logger.exception("Falha ao enviar lembrete por WhatsApp do agendamento %s", agendamento.id)
        registrar_erro_lembrete_whatsapp(db, "Falha de rede ao chamar a Graph API.")
        return False

    if resultado.get("error"):
        erro = resultado["error"]
        mensagem_erro = erro.get("message") if isinstance(erro, dict) else str(erro)
        logger.error("Meta rejeitou o lembrete do agendamento %s: %s", agendamento.id, erro)
        registrar_erro_lembrete_whatsapp(db, mensagem_erro or "Erro desconhecido da Graph API.")
        return False

    limpar_erro_lembrete_whatsapp(db)
    agendamento.lembrete_enviado_em = datetime.utcnow()
    return True


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


async def enviar_lembrete(db, agendamento: Agendamento, config=None) -> bool:
    """Tenta os canais habilitados para a empresa, cada um de forma independente.

    Um canal com falha nunca bloqueia nem repete o outro: cada um grava seu
    próprio carimbo de sucesso (`lembrete_enviado_em` para WhatsApp,
    `lembrete_email_enviado_em` para e-mail), então uma tentativa já bem
    sucedida não é refeita no próximo ciclo do loop de lembretes.
    """
    config = config or obter_configuracao(db)
    empresa = agendamento.empresa
    cliente = agendamento.cliente_final

    tentar_whatsapp = empresa.lembrete_canal_whatsapp
    # Rede de segurança: empresa só quer e-mail, mas esse cliente específico
    # não tem e-mail cadastrado (fluxo normal via WhatsApp nunca coleta isso)
    # — melhor mandar por WhatsApp do que deixar o cliente sem lembrete nenhum.
    if empresa.lembrete_canal_email and not empresa.lembrete_canal_whatsapp and not cliente.email:
        tentar_whatsapp = True

    enviou_algum_canal = False
    alterou_algo = False

    if tentar_whatsapp and agendamento.lembrete_enviado_em is None:
        if await _enviar_lembrete_whatsapp(db, agendamento, config):
            enviou_algum_canal = True
        alterou_algo = True

    if empresa.lembrete_canal_email and agendamento.lembrete_email_enviado_em is None:
        if cliente.email:
            if await _enviar_lembrete_email(db, agendamento):
                enviou_algum_canal = True
            alterou_algo = True
        else:
            # Sem e-mail cadastrado: fecha o ciclo desse canal sem tentar de novo
            # a cada rodada do loop (o WhatsApp, se habilitado, já cobriu acima).
            agendamento.lembrete_email_enviado_em = datetime.utcnow()
            alterou_algo = True

    if alterou_algo:
        db.commit()

    return enviou_algum_canal


async def enviar_lembretes_pendentes(db) -> int:
    config = obter_configuracao(db)
    agendamentos = buscar_agendamentos_para_lembrete(db, config=config)
    enviados = 0
    for agendamento in agendamentos:
        if await enviar_lembrete(db, agendamento, config=config):
            enviados += 1
    return enviados
