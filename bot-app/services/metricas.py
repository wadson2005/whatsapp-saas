from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func

from core.models import Agendamento, ClienteFinal, ConversaIniciada, Empresa, Servico, SolicitacaoAtendimento

from .agenda import PERIODOS

logger = logging.getLogger(__name__)

DIAS_SEMANA_PT = ("Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo")
HORA_INICIO_TARDE = PERIODOS["tarde"][0].hour


def registrar_conversa_iniciada(db, empresa_id: int, telefone: str) -> None:
    """Loga o início de uma conversa nova. Nunca deixa uma falha aqui derrubar o webhook."""
    try:
        db.add(ConversaIniciada(empresa_id=empresa_id, telefone=telefone))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao registrar conversa iniciada (empresa_id=%s)", empresa_id)


def _filtro_empresa(coluna, empresa_id: int | None):
    return [coluna == empresa_id] if empresa_id else []


@dataclass
class MetricasPeriodo:
    data_inicio: datetime
    data_fim: datetime
    conversas_iniciadas: int = 0
    agendamentos_realizados: int = 0
    cancelamentos: int = 0
    solicitacoes_atendimento: int = 0
    clientes_novos: int = 0
    clientes_recorrentes: int = 0
    servico_mais_solicitado: str | None = None
    servico_mais_solicitado_percentual: float = 0.0
    horario_mais_solicitado: str | None = None
    periodo_tarde_percentual: float = 0.0
    dia_semana_mais_movimentado: str | None = None

    @property
    def taxa_conversao(self) -> float:
        if not self.conversas_iniciadas:
            return 0.0
        return self.agendamentos_realizados / self.conversas_iniciadas

    @property
    def taxa_cancelamento(self) -> float:
        total_tentado = self.agendamentos_realizados + self.cancelamentos
        if not total_tentado:
            return 0.0
        return self.cancelamentos / total_tentado


def calcular_metricas(db, empresa_id: int | None, data_inicio: datetime, data_fim: datetime) -> MetricasPeriodo:
    conversas_iniciadas = db.query(func.count(ConversaIniciada.id)).filter(
        ConversaIniciada.criado_em >= data_inicio,
        ConversaIniciada.criado_em <= data_fim,
        *_filtro_empresa(ConversaIniciada.empresa_id, empresa_id),
    ).scalar() or 0

    agendamentos_realizados = db.query(func.count(Agendamento.id)).filter(
        Agendamento.data_hora >= data_inicio,
        Agendamento.data_hora <= data_fim,
        Agendamento.status != "cancelado",
        *_filtro_empresa(Agendamento.empresa_id, empresa_id),
    ).scalar() or 0

    cancelamentos = db.query(func.count(Agendamento.id)).filter(
        Agendamento.cancelado_em.isnot(None),
        Agendamento.cancelado_em >= data_inicio,
        Agendamento.cancelado_em <= data_fim,
        *_filtro_empresa(Agendamento.empresa_id, empresa_id),
    ).scalar() or 0

    solicitacoes_atendimento = db.query(func.count(SolicitacaoAtendimento.id)).filter(
        SolicitacaoAtendimento.criado_em >= data_inicio,
        SolicitacaoAtendimento.criado_em <= data_fim,
        *_filtro_empresa(SolicitacaoAtendimento.empresa_id, empresa_id),
    ).scalar() or 0

    clientes_novos = db.query(func.count(ClienteFinal.id)).filter(
        ClienteFinal.criado_em >= data_inicio,
        ClienteFinal.criado_em <= data_fim,
        *_filtro_empresa(ClienteFinal.empresa_id, empresa_id),
    ).scalar() or 0

    clientes_recorrentes = _contar_clientes_recorrentes(db, empresa_id, data_inicio, data_fim)

    servico_mais_solicitado = None
    servico_mais_solicitado_percentual = 0.0
    horario_mais_solicitado = None
    periodo_tarde_percentual = 0.0
    dia_semana_mais_movimentado = None

    linhas = (
        db.query(Agendamento.data_hora, Servico.nome)
        .join(Servico, Agendamento.servico_id == Servico.id)
        .filter(
            Agendamento.data_hora >= data_inicio,
            Agendamento.data_hora <= data_fim,
            Agendamento.status != "cancelado",
            *_filtro_empresa(Agendamento.empresa_id, empresa_id),
        )
        .all()
    )

    if linhas:
        total_linhas = len(linhas)

        contagem_servicos = Counter(nome for _, nome in linhas)
        servico_mais_solicitado, qtd_servico = contagem_servicos.most_common(1)[0]
        servico_mais_solicitado_percentual = round(100 * qtd_servico / total_linhas, 1)

        contagem_horarios = Counter(data_hora.hour for data_hora, _ in linhas)
        hora_top = contagem_horarios.most_common(1)[0][0]
        horario_mais_solicitado = f"{hora_top:02d}h"

        qtd_tarde = sum(1 for data_hora, _ in linhas if data_hora.hour >= HORA_INICIO_TARDE)
        periodo_tarde_percentual = round(100 * qtd_tarde / total_linhas, 1)

        contagem_dias = Counter(data_hora.weekday() for data_hora, _ in linhas)
        dia_top = contagem_dias.most_common(1)[0][0]
        dia_semana_mais_movimentado = DIAS_SEMANA_PT[dia_top]

    return MetricasPeriodo(
        data_inicio=data_inicio,
        data_fim=data_fim,
        conversas_iniciadas=conversas_iniciadas,
        agendamentos_realizados=agendamentos_realizados,
        cancelamentos=cancelamentos,
        solicitacoes_atendimento=solicitacoes_atendimento,
        clientes_novos=clientes_novos,
        clientes_recorrentes=clientes_recorrentes,
        servico_mais_solicitado=servico_mais_solicitado,
        servico_mais_solicitado_percentual=servico_mais_solicitado_percentual,
        horario_mais_solicitado=horario_mais_solicitado,
        periodo_tarde_percentual=periodo_tarde_percentual,
        dia_semana_mais_movimentado=dia_semana_mais_movimentado,
    )


def _contar_clientes_recorrentes(db, empresa_id: int | None, data_inicio: datetime, data_fim: datetime) -> int:
    """Recorrente = cliente que agendou no período e já tinha mais de 1 agendamento (histórico completo, não cancelado)."""
    cliente_ids_periodo = [
        cid
        for (cid,) in db.query(Agendamento.cliente_final_id)
        .filter(
            Agendamento.data_hora >= data_inicio,
            Agendamento.data_hora <= data_fim,
            Agendamento.status != "cancelado",
            *_filtro_empresa(Agendamento.empresa_id, empresa_id),
        )
        .distinct()
        .all()
    ]
    if not cliente_ids_periodo:
        return 0

    contagens = (
        db.query(Agendamento.cliente_final_id, func.count(Agendamento.id))
        .filter(Agendamento.cliente_final_id.in_(cliente_ids_periodo), Agendamento.status != "cancelado")
        .group_by(Agendamento.cliente_final_id)
        .all()
    )
    return sum(1 for _, total in contagens if total > 1)


def listar_clientes_inativos(db, empresa_id: int | None, dias: int) -> list[dict]:
    corte = datetime.utcnow() - timedelta(days=dias)

    query = (
        db.query(
            ClienteFinal,
            Empresa.nome.label("empresa_nome"),
            func.max(Agendamento.data_hora).label("ultimo_atendimento"),
            func.count(Agendamento.id).label("agendamentos_count"),
        )
        .join(Empresa, ClienteFinal.empresa_id == Empresa.id)
        .outerjoin(Agendamento, Agendamento.cliente_final_id == ClienteFinal.id)
        .filter(*_filtro_empresa(ClienteFinal.empresa_id, empresa_id))
        .group_by(ClienteFinal.id, Empresa.nome)
    )

    resultado = []
    for cliente, empresa_nome, ultimo_atendimento, agendamentos_count in query.all():
        ultima_interacao = ultimo_atendimento or cliente.criado_em
        if ultima_interacao and ultima_interacao < corte:
            resultado.append(
                {
                    "id": cliente.id,
                    "empresa_id": cliente.empresa_id,
                    "empresa_nome": empresa_nome,
                    "nome": cliente.nome,
                    "telefone": cliente.telefone,
                    "ultimo_atendimento": ultimo_atendimento,
                    "agendamentos_count": agendamentos_count,
                }
            )

    resultado.sort(key=lambda item: item["ultimo_atendimento"] or datetime.min)
    return resultado


DIAS_INATIVIDADE_INSIGHT = 90


def gerar_insights(db, empresa_id: int | None) -> list[str]:
    """Frases geradas só a partir de números já calculados — nunca texto ou dado inventado."""
    agora = datetime.utcnow()
    metricas_30d = calcular_metricas(db, empresa_id, agora - timedelta(days=30), agora)

    insights: list[str] = []

    if metricas_30d.servico_mais_solicitado and metricas_30d.agendamentos_realizados:
        insights.append(
            f"O serviço {metricas_30d.servico_mais_solicitado} representa "
            f"{metricas_30d.servico_mais_solicitado_percentual:.0f}% dos agendamentos dos últimos 30 dias."
        )

    if metricas_30d.agendamentos_realizados:
        if metricas_30d.periodo_tarde_percentual >= 50:
            insights.append(f"O período da tarde concentra {metricas_30d.periodo_tarde_percentual:.0f}% dos atendimentos.")
        else:
            insights.append(f"O período da manhã concentra {100 - metricas_30d.periodo_tarde_percentual:.0f}% dos atendimentos.")

    clientes_inativos = listar_clientes_inativos(db, empresa_id, DIAS_INATIVIDADE_INSIGHT)
    if clientes_inativos:
        insights.append(f"{len(clientes_inativos)} clientes não retornam há mais de {DIAS_INATIVIDADE_INSIGHT} dias.")

    metricas_semana_atual = calcular_metricas(db, empresa_id, agora - timedelta(days=7), agora)
    metricas_semana_anterior = calcular_metricas(db, empresa_id, agora - timedelta(days=14), agora - timedelta(days=7))
    if metricas_semana_atual.taxa_cancelamento > 0 or metricas_semana_anterior.taxa_cancelamento > 0:
        if metricas_semana_atual.taxa_cancelamento > metricas_semana_anterior.taxa_cancelamento:
            insights.append("O índice de cancelamento aumentou nesta semana em relação à semana anterior.")
        elif metricas_semana_atual.taxa_cancelamento < metricas_semana_anterior.taxa_cancelamento:
            insights.append("O índice de cancelamento diminuiu nesta semana em relação à semana anterior.")

    return insights
