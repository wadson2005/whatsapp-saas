from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import joinedload

from models import Agendamento, ClienteFinal, Empresa, Servico


DEFAULT_HORARIO_ABERTURA = time(8, 0)
DEFAULT_HORARIO_FECHAMENTO = time(18, 0)
DEFAULT_INTERVALO_MINUTOS = 15
LOOKAHEAD_PADRAO_DIAS = 14
PERIODOS = {
    "manha": (time(8, 0), time(12, 0)),
    "tarde": (time(12, 0), time(18, 0)),
}


@dataclass(frozen=True)
class SlotDisponivel:
    inicio_em: datetime
    fim_em: datetime

    @property
    def id(self) -> str:
        return self.inicio_em.isoformat(timespec="minutes")

    @property
    def titulo(self) -> str:
        return self.inicio_em.strftime("%d/%m %H:%M")

    @property
    def descricao(self) -> str:
        return self.fim_em.strftime("até %H:%M")


@dataclass
class ValidacaoAgendamento:
    ok: bool
    mensagem: str
    sugestoes: list[SlotDisponivel] = field(default_factory=list)


def _primeiro_texto(valor, padrao: str) -> str:
    texto = (valor or "").strip()
    return texto if texto else padrao


def _primeiro_inteiro(valor, padrao: int) -> int:
    try:
        if valor is None:
            raise ValueError
        return int(valor)
    except (TypeError, ValueError):
        return padrao


def _parse_lista_inteiros(valor) -> set[int]:
    texto = (valor or "").strip()
    if not texto:
        return set()
    itens = set()
    for parte in texto.split(","):
        parte = parte.strip()
        if not parte:
            continue
        try:
            itens.add(int(parte))
        except ValueError:
            continue
    return itens


def _parse_lista_datas(valor) -> set[date]:
    texto = (valor or "").strip()
    if not texto:
        return set()
    itens = set()
    for parte in texto.split(","):
        parte = parte.strip()
        if not parte:
            continue
        try:
            itens.add(datetime.strptime(parte, "%Y-%m-%d").date())
        except ValueError:
            continue
    return itens


def _parse_hora(valor, padrao: time) -> time:
    texto = _primeiro_texto(valor, padrao.strftime("%H:%M"))
    try:
        return datetime.strptime(texto, "%H:%M").time()
    except ValueError:
        return padrao


def horario_abertura(empresa: Empresa) -> time:
    return _parse_hora(empresa.horario_abertura, DEFAULT_HORARIO_ABERTURA)


def horario_fechamento(empresa: Empresa) -> time:
    return _parse_hora(empresa.horario_fechamento, DEFAULT_HORARIO_FECHAMENTO)


def intervalo_entre_atendimentos(empresa: Empresa) -> int:
    return max(_primeiro_inteiro(empresa.intervalo_entre_atendimentos_minutos, DEFAULT_INTERVALO_MINUTOS), 0)


def duracao_servico(servico: Servico) -> int:
    return max(_primeiro_inteiro(servico.duracao_minutos, 30), 1)


def dias_indisponiveis(empresa: Empresa) -> set[int]:
    return _parse_lista_inteiros(empresa.dias_indisponiveis)


def datas_indisponiveis(empresa: Empresa) -> set[date]:
    return _parse_lista_datas(empresa.datas_indisponiveis)


def _faixa_periodo(periodo: str | None) -> tuple[time, time] | None:
    if not periodo:
        return None
    chave = periodo.strip().lower()
    return PERIODOS.get(chave)


def _data_disponivel(empresa: Empresa, dia: date) -> bool:
    return dia.weekday() not in dias_indisponiveis(empresa) and dia not in datas_indisponiveis(empresa)


def _intervalo_agendamento(agendamento: Agendamento, servico: Servico | None = None) -> tuple[datetime, datetime]:
    inicio = agendamento.data_hora
    duracao = agendamento.duracao_minutos or (servico.duracao_minutos if servico else 30)
    fim = agendamento.fim_em or (inicio + timedelta(minutes=duracao))
    return inicio, fim


def _conflicta(inicio: datetime, fim: datetime, bloqueio_inicio: datetime, bloqueio_fim: datetime, buffer_minutos: int) -> bool:
    margem = timedelta(minutes=buffer_minutos)
    return not (fim <= bloqueio_inicio - margem or inicio >= bloqueio_fim + margem)


def _agendamentos_em_janela(db, empresa_id: int, inicio: datetime, fim: datetime, ignorar_agendamento_id: int | None = None) -> list[Agendamento]:
    query = (
        db.query(Agendamento)
        .options(joinedload(Agendamento.servico))
        .filter(
            Agendamento.empresa_id == empresa_id,
            Agendamento.status != "cancelado",
            Agendamento.data_hora < fim,
        )
    )
    if ignorar_agendamento_id:
        query = query.filter(Agendamento.id != ignorar_agendamento_id)
    return query.all()


def _slot_disponivel(
    inicio: datetime,
    fim: datetime,
    agendamentos: list[Agendamento],
    buffer_minutos: int,
    servico: Servico,
) -> bool:
    for agendamento in agendamentos:
        bloqueio_inicio, bloqueio_fim = _intervalo_agendamento(agendamento, agendamento.servico)
        if _conflicta(inicio, fim, bloqueio_inicio, bloqueio_fim, buffer_minutos):
            return False
    return True


def obter_slots_disponiveis(
    db,
    empresa: Empresa,
    servico: Servico,
    periodo: str | None = None,
    inicio_busca: datetime | None = None,
    dias_busca: int = LOOKAHEAD_PADRAO_DIAS,
    limite: int = 12,
    ignorar_agendamento_id: int | None = None,
) -> list[SlotDisponivel]:
    abertura = horario_abertura(empresa)
    fechamento = horario_fechamento(empresa)
    buffer_minutos = intervalo_entre_atendimentos(empresa)
    passo = timedelta(minutes=max(buffer_minutos, 5))
    duracao = timedelta(minutes=duracao_servico(servico))
    inicio_base = inicio_busca or datetime.utcnow()
    periodo_faixa = _faixa_periodo(periodo)
    agendamentos = _agendamentos_em_janela(
        db,
        empresa.id,
        inicio_base - timedelta(days=1),
        inicio_base + timedelta(days=dias_busca + 1),
        ignorar_agendamento_id=ignorar_agendamento_id,
    )

    slots: list[SlotDisponivel] = []
    data_corrente = inicio_base.date()
    for offset in range(dias_busca + 1):
        dia = data_corrente + timedelta(days=offset)
        if not _data_disponivel(empresa, dia):
            continue

        inicio_dia = datetime.combine(dia, abertura)
        fim_dia = datetime.combine(dia, fechamento)

        if periodo_faixa:
            periodo_inicio, periodo_fim = periodo_faixa
            inicio_dia = max(inicio_dia, datetime.combine(dia, periodo_inicio))
            fim_dia = min(fim_dia, datetime.combine(dia, periodo_fim))

        cursor = inicio_dia
        while cursor + duracao <= fim_dia:
            if cursor < inicio_base:
                cursor += passo
                continue

            fim_slot = cursor + duracao
            if _slot_disponivel(cursor, fim_slot, agendamentos, buffer_minutos, servico):
                slots.append(SlotDisponivel(cursor, fim_slot))
                if len(slots) >= limite:
                    return slots

            cursor += passo

    return slots


def validar_agendamento(
    db,
    empresa: Empresa,
    servico: Servico,
    inicio_em: datetime,
    ignorar_agendamento_id: int | None = None,
) -> ValidacaoAgendamento:
    if not empresa.ativo:
        return ValidacaoAgendamento(False, "Esta empresa está inativa no momento.")

    if not servico.ativo:
        return ValidacaoAgendamento(False, "Este serviço está indisponível no momento.")

    if not _data_disponivel(empresa, inicio_em.date()):
        return ValidacaoAgendamento(
            False,
            "Este dia está indisponível para atendimento. Escolha outra data.",
            obter_slots_disponiveis(db, empresa, servico, inicio_busca=inicio_em, ignorar_agendamento_id=ignorar_agendamento_id),
        )

    abertura = horario_abertura(empresa)
    fechamento = horario_fechamento(empresa)
    buffer_minutos = intervalo_entre_atendimentos(empresa)
    duracao = timedelta(minutes=duracao_servico(servico))
    inicio_dia = datetime.combine(inicio_em.date(), abertura)
    fim_dia = datetime.combine(inicio_em.date(), fechamento)
    fim_em = inicio_em + duracao

    if inicio_em < datetime.utcnow():
        return ValidacaoAgendamento(
            False,
            "Esse horário já passou. Escolha um horário futuro.",
            obter_slots_disponiveis(db, empresa, servico, inicio_busca=datetime.utcnow(), ignorar_agendamento_id=ignorar_agendamento_id),
        )

    if inicio_em < inicio_dia or fim_em > fim_dia:
        return ValidacaoAgendamento(
            False,
            f"Fora do horário de funcionamento. Atendemos entre {abertura.strftime('%H:%M')} e {fechamento.strftime('%H:%M')}",
            obter_slots_disponiveis(db, empresa, servico, inicio_busca=inicio_em, ignorar_agendamento_id=ignorar_agendamento_id),
        )

    agendamentos = _agendamentos_em_janela(
        db,
        empresa.id,
        inicio_em - timedelta(days=1),
        fim_em + timedelta(days=1),
        ignorar_agendamento_id=ignorar_agendamento_id,
    )
    for agendamento in agendamentos:
        bloqueio_inicio, bloqueio_fim = _intervalo_agendamento(agendamento, agendamento.servico)
        if _conflicta(inicio_em, fim_em, bloqueio_inicio, bloqueio_fim, buffer_minutos):
            return ValidacaoAgendamento(
                False,
                "Esse horário já está ocupado. Escolha outro horário.",
                obter_slots_disponiveis(db, empresa, servico, inicio_busca=inicio_em, ignorar_agendamento_id=ignorar_agendamento_id),
            )

    return ValidacaoAgendamento(True, "Horário disponível.")


def parsear_data_hora_texto(texto: str, base: datetime | None = None) -> datetime | None:
    base = base or datetime.utcnow()
    bruto = texto.strip().lower()
    bruto = bruto.replace("às", " ")
    bruto = bruto.replace("hs", "h")
    bruto = bruto.replace("horas", "h")
    bruto = re.sub(r"\b(\d{1,2})h\b", r"\1:00", bruto)
    bruto = re.sub(r"\b(\d{1,2})h(\d{2})\b", r"\1:\2", bruto)

    amanha = "amanha" in bruto or "amanhã" in bruto
    hoje = "hoje" in bruto
    bruto = bruto.replace("amanha", "").replace("amanhã", "").replace("hoje", "")
    bruto = re.sub(r"\s+", " ", bruto).strip()

    formatos = ["%d/%m/%Y %H:%M", "%d/%m/%Y %H", "%d/%m %H:%M", "%d/%m %H"]
    for formato in formatos:
        try:
            parsed = datetime.strptime(bruto, formato)
            if "%Y" not in formato:
                parsed = parsed.replace(year=base.year)
                if parsed < base and not hoje and not amanha:
                    parsed = parsed.replace(year=base.year + 1)
            if amanha:
                parsed = parsed.replace(year=base.year, month=base.month, day=base.day) + timedelta(days=1)
                if ":" in bruto:
                    hora_texto = bruto.split()[-1]
                    parsed = parsed.replace(hour=int(hora_texto.split(":")[0]), minute=int(hora_texto.split(":")[1]) if ":" in hora_texto else 0)
            if hoje:
                parsed = parsed.replace(year=base.year, month=base.month, day=base.day)
                if ":" in bruto:
                    hora_texto = bruto.split()[-1]
                    parsed = parsed.replace(hour=int(hora_texto.split(":")[0]), minute=int(hora_texto.split(":")[1]) if ":" in hora_texto else 0)
            return parsed
        except ValueError:
            continue
    return None


def formatar_data_hora(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y às %H:%M")


def agendar_servico(
    db,
    empresa: Empresa,
    servico: Servico,
    numero: str,
    inicio_em: datetime,
) -> tuple[Agendamento | None, ValidacaoAgendamento]:
    validacao = validar_agendamento(db, empresa, servico, inicio_em)
    if not validacao.ok:
        return None, validacao

    numero_limpo = numero.split("@")[0]
    cliente = db.query(ClienteFinal).filter_by(empresa_id=empresa.id, telefone=numero_limpo).first()
    if not cliente:
        cliente = ClienteFinal(empresa_id=empresa.id, telefone=numero_limpo)
        db.add(cliente)
        db.flush()

    fim_em = inicio_em + timedelta(minutes=duracao_servico(servico))
    agendamento = Agendamento(
        empresa_id=empresa.id,
        cliente_final_id=cliente.id,
        servico_id=servico.id,
        data_hora=inicio_em,
        fim_em=fim_em,
        duracao_minutos=duracao_servico(servico),
        status="confirmado",
    )
    db.add(agendamento)
    db.commit()
    db.refresh(agendamento)
    return agendamento, validacao


def reagendar_agendamento(
    db,
    empresa: Empresa,
    agendamento: Agendamento,
    inicio_em: datetime,
) -> tuple[Agendamento | None, ValidacaoAgendamento]:
    servico = agendamento.servico
    validacao = validar_agendamento(db, empresa, servico, inicio_em, ignorar_agendamento_id=agendamento.id)
    if not validacao.ok:
        return None, validacao

    agendamento.data_hora = inicio_em
    agendamento.fim_em = inicio_em + timedelta(minutes=duracao_servico(servico))
    agendamento.duracao_minutos = duracao_servico(servico)
    agendamento.status = "confirmado"
    db.commit()
    db.refresh(agendamento)
    return agendamento, validacao


def cancelar_agendamento(db, agendamento: Agendamento, motivo: str | None = None) -> Agendamento:
    agendamento.status = "cancelado"
    agendamento.cancelado_em = datetime.utcnow()
    agendamento.motivo_cancelamento = motivo
    db.commit()
    db.refresh(agendamento)
    return agendamento