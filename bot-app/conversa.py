from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, time

from ai.models import Intent
from ai.service import criar_ai_service
from core.models import Agendamento, ClienteFinal, Empresa, Servico
from core.redis_client import redis_cliente
from integrations.meta_client import enviar_botoes, enviar_lista
from services.agenda import (
    agendar_servico,
    cancelar_agendamento,
    formatar_data_hora,
    obter_slots_disponiveis,
    parsear_data_hora_texto,
    reagendar_agendamento,
)
from services.atendimento_humano import registrar_solicitacao_atendimento
from services.conhecimento import buscar_resposta
from services.configuracoes import obter_configuracao, parse_activation_words
from services.metricas import registrar_conversa_iniciada
from services.texto_utils import normalizar_texto

logger = logging.getLogger(__name__)

TEMPO_EXPIRACAO_SEGUNDOS = 30 * 60
LIMITE_SLOTS_EXIBIDOS = 10

TEXTO_BOTAO_MANHA = "Manhã"
TEXTO_BOTAO_TARDE = "Tarde"
TEXTO_BOTAO_OUTRO = "Prefiro digitar"
TEXTO_BOTAO_VER_SERVICOS = "Ver serviços de novo"
TEXTO_BOTAO_MENU = "Menu"
TEXTO_BOTAO_REAGENDAR = "Reagendar"
TEXTO_BOTAO_CANCELAR_AGENDAMENTO = "Cancelar agendamento"
TEXTO_BOTAO_CONFIRMAR_CANCELAMENTO = "Sim, cancelar"
TEXTO_BOTAO_MANTER_AGENDAMENTO = "Não, manter"
TEXTO_BOTAO_FALAR_COM_ATENDENTE = "Falar com atendente"

SAUDACOES_FIXAS = ("oi", "ola", "bom dia", "boa tarde", "boa noite")
PALAVRAS_MENU = ("menu", "voltar", "inicio", "iniciar", "comecar")
PALAVRAS_CANCELAMENTO = ("cancelar agendamento", "desmarcar", "cancelar")
PALAVRAS_REAGENDAMENTO = ("reagendar", "remarcar")
PALAVRAS_HUMANO = ("atendente", "atendimento humano", "falar com atendente", "falar com humano", "pessoa")
PALAVRAS_CONFIRMACAO = ("sim", "confirmar", "pode cancelar")
PALAVRAS_NEGACAO = ("nao", "não", "manter", "voltar")


def _numero_limpo(numero: str) -> str:
    return numero.split("@")[0]


def _texto_corresponde(texto: str, opcoes: tuple[str, ...]) -> bool:
    normalizado = normalizar_texto(texto)
    return any(re.search(rf"\b{re.escape(opcao)}\b", normalizado) for opcao in opcoes)


def _parse_horario(valor: str | None, padrao: str) -> time:
    texto = (valor or "").strip() or padrao
    hora, minuto = texto.split(":")
    return time(int(hora), int(minuto))


def _empresa_mensagem(empresa: Empresa, atributo: str, padrao: str) -> str:
    texto = getattr(empresa, atributo, None)
    return texto.strip() if isinstance(texto, str) and texto.strip() else padrao


def _empresa_horario_disponivel(empresa: Empresa) -> bool:
    if not empresa.atendimento_automatico_ativo:
        return False

    agora = datetime.now().time()
    inicio = _parse_horario(empresa.horario_resposta_inicio, "08:00")
    fim = _parse_horario(empresa.horario_resposta_fim, "18:00")
    if inicio <= fim and not (inicio <= agora <= fim):
        return False

    almoco_inicio = empresa.horario_almoco_inicio
    almoco_fim = empresa.horario_almoco_fim
    if almoco_inicio and almoco_fim:
        inicio_almoco = _parse_horario(almoco_inicio, "12:00")
        fim_almoco = _parse_horario(almoco_fim, "13:00")
        if inicio_almoco <= agora <= fim_almoco:
            return False

    dias_funcionamento = empresa.dias_funcionamento
    if dias_funcionamento:
        dias = {
            int(parte.strip())
            for parte in dias_funcionamento.split(",")
            if parte.strip().isdigit()
        }
        if dias and datetime.now().weekday() not in dias:
            return False

    return True


def _ttl_contexto(empresa: Empresa) -> int:
    minutos = empresa.tempo_expiracao_contexto_minutos or 30
    return max(int(minutos), 1) * 60


def _max_conversa_segundos(empresa: Empresa) -> int:
    minutos = empresa.tempo_max_conversa_minutos or 120
    return max(int(minutos), 1) * 60


def _chave_estado(empresa_id: int, numero: str) -> str:
    return f"conversa:{empresa_id}:{numero}"


def obter_estado(empresa_id: int, numero: str) -> dict:
    bruto = redis_cliente.get(_chave_estado(empresa_id, numero))
    if bruto:
        return json.loads(bruto)
    return {"passo": "novo", "contexto": {}}


def salvar_estado(empresa_id: int, numero: str, passo: str, contexto: dict, ttl_segundos: int | None = None):
    estado_atual = obter_estado(empresa_id, numero)
    inicio_em = estado_atual.get("inicio_em") or datetime.utcnow().isoformat()
    dado = json.dumps({"passo": passo, "contexto": contexto, "inicio_em": inicio_em})
    redis_cliente.set(_chave_estado(empresa_id, numero), dado, ex=ttl_segundos or TEMPO_EXPIRACAO_SEGUNDOS)


def limpar_estado(empresa_id: int, numero: str):
    redis_cliente.delete(_chave_estado(empresa_id, numero))


def _texto_bate(texto: str, alvo: str) -> bool:
    return texto.strip().lower() == alvo.strip().lower()


def _texto_id_bate(id_interacao: str | None, prefixo: str) -> bool:
    return bool(id_interacao and id_interacao.startswith(prefixo))


def _agendamento_do_contexto(db, empresa: Empresa, numero: str, contexto: dict):
    agendamento_id = contexto.get("agendamento_id")
    if agendamento_id:
        agendamento = db.query(Agendamento).filter_by(id=agendamento_id, empresa_id=empresa.id).first()
        if agendamento:
            return agendamento

    return _agendamento_ativo_do_numero(db, empresa, numero)


def _id_slot_para_datetime(id_interacao: str | None) -> datetime | None:
    if not id_interacao or not id_interacao.startswith("slot:"):
        return None
    _, valor = id_interacao.split("slot:", 1)
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None


def _obter_servico_por_contexto(db, contexto: dict):
    servico_id = contexto.get("servico_id")
    if not servico_id:
        return None
    return db.query(Servico).filter_by(id=servico_id, excluido_em=None).first()


def _agendamento_ativo_do_numero(db, empresa: Empresa, numero: str):
    numero_limpo = _numero_limpo(numero)
    cliente = (
        db.query(ClienteFinal)
        .filter_by(empresa_id=empresa.id, telefone=numero_limpo)
        .order_by(ClienteFinal.id.desc())
        .first()
    )
    if not cliente:
        return None

    return (
        db.query(Agendamento)
        .filter(
            Agendamento.empresa_id == empresa.id,
            Agendamento.cliente_final_id == cliente.id,
            Agendamento.status != "cancelado",
        )
        .order_by(Agendamento.data_hora.desc())
        .first()
    )


def _agrupa_slots_por_dia(slots):
    secoes = []
    atual = None
    linhas = []

    for slot in slots:
        data_slot = slot.inicio_em.strftime("%d/%m/%Y")
        if atual != data_slot:
            if linhas:
                secoes.append({"titulo": atual, "linhas": linhas})
            atual = data_slot
            linhas = []

        linhas.append(
            {
                "id": f"slot:{slot.id}",
                "titulo": slot.titulo,
                "descricao": f"Até {slot.fim_em.strftime('%H:%M')}",
            }
        )

    if linhas:
        secoes.append({"titulo": atual, "linhas": linhas})

    return secoes


async def _mostrar_menu_principal(db, empresa: Empresa, numero: str, contexto: dict | None = None):
    agendamento = _agendamento_do_contexto(db, empresa, numero, contexto or {})
    if agendamento:
        texto = (
            f"Posso te ajudar com o agendamento de *{agendamento.servico.nome if agendamento.servico else 'seu serviço'}* em "
            f"*{formatar_data_hora(agendamento.data_hora)}*.\n\n"
            "Escolha o próximo passo no menu abaixo."
        )
    else:
        texto = _empresa_mensagem(
            empresa,
            "mensagem_boas_vindas",
            f"Olá! Sou a assistente da {empresa.nome}.\n\nPosso ajudar a ver serviços, reagendar, cancelar ou encaminhar você para um atendente.",
        )

    atalhos = [
        {"id": "menu:servicos", "titulo": "Ver serviços", "descricao": "Começar um novo agendamento"},
        {"id": "agendamento:reagendar", "titulo": "Reagendar agendamento", "descricao": "Trocar o horário marcado"},
        {"id": "agendamento:cancelar", "titulo": "Cancelar agendamento", "descricao": "Cancelar um horário existente"},
    ]
    if empresa.permitir_atendimento_humano:
        atalhos.append({"id": "atendimento:humano", "titulo": TEXTO_BOTAO_FALAR_COM_ATENDENTE, "descricao": "Falar com uma pessoa"})

    await enviar_lista(
        numero=_numero_limpo(numero),
        texto=texto,
        titulo_botao="Abrir menu",
        secoes=[{"titulo": "Atalhos rápidos", "linhas": atalhos}],
        rodape="Você também pode digitar Menu a qualquer momento.",
    )


async def _mostrar_atendimento_humano(
    db,
    empresa: Empresa,
    numero: str,
    texto: str,
    contexto: dict | None = None,
):
    if not empresa.permitir_atendimento_humano:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=_empresa_mensagem(
                empresa,
                "mensagem_fora_horario",
                "O atendimento humano está indisponível neste momento. Vou te orientar pelo bot.",
            ),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    agendamento = _agendamento_do_contexto(db, empresa, numero, contexto or {})
    mensagem = (texto or "Solicitação de atendimento humano via WhatsApp").strip()
    if agendamento:
        mensagem = (
            f"{mensagem}\n\n"
            f"Contexto atual: *{agendamento.servico.nome if agendamento.servico else 'serviço'}* em *{formatar_data_hora(agendamento.data_hora)}*."
        )

    _solicitacao, criada = registrar_solicitacao_atendimento(
        db,
        empresa,
        numero,
        mensagem,
        nome=None,
    )
    limpar_estado(empresa.id, numero)

    if criada:
        resposta = _empresa_mensagem(
            empresa,
            "mensagem_atendimento_humano",
            "Pronto, registrei sua solicitação de atendimento humano. Nossa equipe vai ver isso em breve.",
        )
    else:
        resposta = "Sua solicitação de atendimento humano já está registrada. Nossa equipe vai ver isso em breve."

    texto = (
        f"{resposta}\n\n"
        "Quando quiser continuar pelo bot, envie Menu."
    )

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=texto,
        botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
    )


async def _solicitar_confirmacao_cancelamento(db, empresa: Empresa, numero: str, contexto: dict):
    agendamento = _agendamento_do_contexto(db, empresa, numero, contexto)
    if not agendamento:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=_empresa_mensagem(empresa, "mensagem_encerramento", "Não encontrei um agendamento ativo para cancelar."),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    salvar_estado(
        empresa.id,
        numero,
        "aguardando_cancelamento_confirmacao",
        {"agendamento_id": agendamento.id, "servico_id": agendamento.servico_id},
        ttl_segundos=_ttl_contexto(empresa),
    )

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=(
            f"Você quer mesmo cancelar *{agendamento.servico.nome if agendamento.servico else 'o agendamento'}* em "
            f"*{formatar_data_hora(agendamento.data_hora)}*?\n\n"
            "Essa ação pode ser desfeita só reagendando depois."
        ),
        botoes=[
            {"id": "cancelamento:confirmar", "titulo": TEXTO_BOTAO_CONFIRMAR_CANCELAMENTO},
            {"id": "cancelamento:manter", "titulo": TEXTO_BOTAO_MANTER_AGENDAMENTO},
            {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
        ],
    )


async def _confirmar_cancelamento(db, empresa: Empresa, numero: str, contexto: dict):
    agendamento = _agendamento_do_contexto(db, empresa, numero, contexto)
    if not agendamento:
        limpar_estado(empresa.id, numero)
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=_empresa_mensagem(empresa, "mensagem_encerramento", "Não encontrei um agendamento ativo para cancelar."),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    cancelar_agendamento(db, agendamento, motivo="cancelado pelo cliente via bot")
    limpar_estado(empresa.id, numero)

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=(
            f"Agendamento de *{agendamento.servico.nome if agendamento.servico else 'serviço'}* em "
            f"*{formatar_data_hora(agendamento.data_hora)}* cancelado com sucesso."
        ),
        botoes=[
            {"id": "menu:servicos", "titulo": TEXTO_BOTAO_VER_SERVICOS},
            {"id": "atendimento:humano", "titulo": TEXTO_BOTAO_FALAR_COM_ATENDENTE},
            {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
        ],
    )


async def _manter_agendamento(db, empresa: Empresa, numero: str, contexto: dict):
    agendamento = _agendamento_do_contexto(db, empresa, numero, contexto)
    limpar_estado(empresa.id, numero)
    if agendamento:
        texto = (
            "Perfeito, mantive seu agendamento como está.\n\n"
            f"Atual: *{agendamento.servico.nome if agendamento.servico else 'serviço'}* em *{formatar_data_hora(agendamento.data_hora)}*."
        )
    else:
        texto = "Perfeito, mantive sua solicitação como está."

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=texto,
        botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
    )


def _resposta_fallback(passo: str) -> tuple[str, list[dict]]:
    if passo == "aguardando_servico":
        return (
            "Não consegui identificar o serviço. Toque em uma opção da lista, volte ao menu ou fale com um atendente.",
            [
                {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
                {"id": "menu:servicos", "titulo": TEXTO_BOTAO_VER_SERVICOS},
                {"id": "atendimento:humano", "titulo": TEXTO_BOTAO_FALAR_COM_ATENDENTE},
            ],
        )

    if passo in {"aguardando_periodo", "aguardando_horario_texto"}:
        return (
            "Não entendi essa resposta. Você pode escolher manhã, tarde, digitar uma data, voltar ao menu ou falar com um atendente.",
            [
                {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
                {"id": "atendimento:humano", "titulo": TEXTO_BOTAO_FALAR_COM_ATENDENTE},
            ],
        )

    if passo in {"aguardando_slot", "aguardando_reagendamento_slot", "aguardando_reagendamento_texto"}:
        return (
            "Não consegui entender esse horário. Escolha uma opção da lista, digite a data no formato DD/MM/AAAA HH:MM ou volte ao menu.",
            [
                {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
                {"id": "atendimento:humano", "titulo": TEXTO_BOTAO_FALAR_COM_ATENDENTE},
            ],
        )

    if passo == "aguardando_cancelamento_confirmacao":
        return (
            "Só preciso da sua confirmação para concluir o cancelamento.",
            [
                {"id": "cancelamento:confirmar", "titulo": TEXTO_BOTAO_CONFIRMAR_CANCELAMENTO},
                {"id": "cancelamento:manter", "titulo": TEXTO_BOTAO_MANTER_AGENDAMENTO},
                {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
            ],
        )

    return (
        "Não entendi sua mensagem. Posso te ajudar pelo menu com agendamento, reagendamento, cancelamento ou atendimento humano.",
        [
            {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
            {"id": "menu:servicos", "titulo": TEXTO_BOTAO_VER_SERVICOS},
            {"id": "atendimento:humano", "titulo": TEXTO_BOTAO_FALAR_COM_ATENDENTE},
        ],
    )


async def _mostrar_lista_servicos(db, empresa: Empresa, numero: str):
    servicos = (
        db.query(Servico)
        .filter_by(empresa_id=empresa.id, ativo=True)
        .filter(Servico.excluido_em.is_(None))
        .order_by(Servico.ordem_exibicao.asc(), Servico.nome.asc())
        .all()
    )
    if not servicos:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=(
                f"Olá! No momento a {empresa.nome} não tem serviços ativos cadastrados.\n\n"
                "Peça para o administrador liberar a oferta antes de agendar."
            ),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}] + ([{"id": "atendimento:humano", "titulo": TEXTO_BOTAO_FALAR_COM_ATENDENTE}] if empresa.permitir_atendimento_humano else []),
        )
        return

    secoes = [
        {
            "titulo": "Serviços disponíveis",
            "linhas": [
                {
                    "id": f"servico:{s.id}",
                    "titulo": s.nome,
                    "descricao": f"R$ {s.preco:.2f} · {s.duracao_minutos} min" if s.preco is not None else f"{s.duracao_minutos} min",
                }
                for s in servicos
            ],
        }
    ]

    await enviar_lista(
        numero=_numero_limpo(numero),
        texto=_empresa_mensagem(
            empresa,
            "mensagem_boas_vindas",
            f"Olá! Bem-vindo(a) à {empresa.nome} 😊\n\nEscolha um serviço para agendar:",
        ),
        titulo_botao="Ver serviços",
        secoes=secoes,
        rodape="Toque em uma opção da lista",
    )

    salvar_estado(empresa.id, numero, "aguardando_servico", {"servicos_ids": [s.id for s in servicos]}, ttl_segundos=_ttl_contexto(empresa))


async def _servico_escolhido(db, empresa: Empresa, numero: str, texto: str, contexto: dict, id_interacao: str | None):
    servicos_ids = contexto.get("servicos_ids", [])
    servico = None

    if _texto_id_bate(id_interacao, "servico:"):
        try:
            servico_id = int(id_interacao.split(":", 1)[1])
        except ValueError:
            servico_id = None
        if servico_id in servicos_ids:
            servico = db.query(Servico).filter_by(id=servico_id, empresa_id=empresa.id, ativo=True).first()

    if not servico and servicos_ids:
        candidatos = db.query(Servico).filter(Servico.id.in_(servicos_ids)).all()
        for s in candidatos:
            if _texto_bate(texto, s.nome):
                servico = s
                break

    if not servico:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Por favor, toque em uma das opções da lista para escolher o serviço.",
            botoes=[{"id": "ver_servicos", "titulo": TEXTO_BOTAO_VER_SERVICOS}],
        )
        return

    contexto = {"servico_id": servico.id}
    salvar_estado(empresa.id, numero, "aguardando_periodo", contexto, ttl_segundos=_ttl_contexto(empresa))

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=f"Você escolheu: {servico.nome}.\n\nQual período prefere?",
        botoes=[
            {"id": "periodo:manha", "titulo": TEXTO_BOTAO_MANHA},
            {"id": "periodo:tarde", "titulo": TEXTO_BOTAO_TARDE},
            {"id": "periodo:outro", "titulo": TEXTO_BOTAO_OUTRO},
        ],
        rodape="Depois confirmamos um horário real disponível",
    )


async def _periodo_escolhido(db, empresa: Empresa, numero: str, texto: str, contexto: dict, id_interacao: str | None):
    servico = _obter_servico_por_contexto(db, contexto)
    if not servico:
        limpar_estado(empresa.id, numero)
        return

    if _texto_bate(texto, TEXTO_BOTAO_OUTRO) or id_interacao == "periodo:outro":
        salvar_estado(empresa.id, numero, "aguardando_horario_texto", contexto, ttl_segundos=_ttl_contexto(empresa))
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=(
                "Sem problemas. Envie a data e a hora no formato:\n"
                "DD/MM/AAAA HH:MM ou DD/MM HH:MM\n\n"
                "Exemplo: 29/07/2026 15:00"
            ),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    periodo = None
    if _texto_bate(texto, TEXTO_BOTAO_MANHA) or id_interacao == "periodo:manha":
        periodo = "manha"
    elif _texto_bate(texto, TEXTO_BOTAO_TARDE) or id_interacao == "periodo:tarde":
        periodo = "tarde"

    if not periodo:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Por favor, toque em uma das opções abaixo.",
            botoes=[
                {"id": "periodo:manha", "titulo": TEXTO_BOTAO_MANHA},
                {"id": "periodo:tarde", "titulo": TEXTO_BOTAO_TARDE},
                {"id": "periodo:outro", "titulo": TEXTO_BOTAO_OUTRO},
            ],
        )
        return

    slots = obter_slots_disponiveis(db, empresa, servico, periodo=periodo, limite=LIMITE_SLOTS_EXIBIDOS)
    if not slots:
        salvar_estado(empresa.id, numero, "aguardando_periodo", contexto, ttl_segundos=_ttl_contexto(empresa))
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=(
                f"Não encontrei horários disponíveis para {periodo} nos próximos dias.\n\n"
                f"Horário de funcionamento: {empresa.horario_abertura or '08:00'} às {empresa.horario_fechamento or '18:00'}."
            ),
            botoes=[
                {"id": "periodo:manha", "titulo": TEXTO_BOTAO_MANHA},
                {"id": "periodo:tarde", "titulo": TEXTO_BOTAO_TARDE},
                {"id": "periodo:outro", "titulo": TEXTO_BOTAO_OUTRO},
            ],
        )
        return

    slots_map = [slot.id for slot in slots]
    salvar_estado(
        empresa.id,
        numero,
        "aguardando_slot",
        {"servico_id": servico.id, "periodo": periodo, "slots": slots_map},
        ttl_segundos=_ttl_contexto(empresa),
    )

    await enviar_lista(
        numero=_numero_limpo(numero),
        texto=(
            f"Horários disponíveis para {servico.nome} ({'manhã' if periodo == 'manha' else 'tarde'}).\n"
            "Escolha um horário para concluir o agendamento:"
        ),
        titulo_botao="Ver horários",
        secoes=_agrupa_slots_por_dia(slots),
        rodape="Toque no horário desejado",
    )


async def _slot_escolhido(db, empresa: Empresa, numero: str, texto: str, contexto: dict, id_interacao: str | None):
    servico = _obter_servico_por_contexto(db, contexto)
    if not servico:
        limpar_estado(empresa.id, numero)
        return

    inicio_em = _id_slot_para_datetime(id_interacao)
    if inicio_em is None:
        inicio_em = parsear_data_hora_texto(texto)

    if inicio_em is None:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=(
                "Não consegui entender esse horário.\n"
                "Use o botão de horários ou envie a data no formato DD/MM/AAAA HH:MM."
            ),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    agendamento, validacao = agendar_servico(db, empresa, servico, numero, inicio_em)

    if not validacao.ok:
        if validacao.sugestoes:
            salvar_estado(empresa.id, numero, "aguardando_slot", contexto, ttl_segundos=_ttl_contexto(empresa))
            await enviar_lista(
                numero=_numero_limpo(numero),
                texto=validacao.mensagem,
                titulo_botao="Ver alternativas",
                secoes=_agrupa_slots_por_dia(validacao.sugestoes),
                rodape="Escolha um horário livre",
            )
        else:
            salvar_estado(empresa.id, numero, "aguardando_periodo", contexto, ttl_segundos=_ttl_contexto(empresa))
            await enviar_botoes(
                numero=_numero_limpo(numero),
                texto=validacao.mensagem,
                botoes=[
                    {"id": "periodo:manha", "titulo": TEXTO_BOTAO_MANHA},
                    {"id": "periodo:tarde", "titulo": TEXTO_BOTAO_TARDE},
                    {"id": "periodo:outro", "titulo": TEXTO_BOTAO_OUTRO},
                ],
            )
        return

    if not agendamento:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Não foi possível concluir o agendamento. Tente novamente em instantes.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    salvar_estado(
        empresa.id,
        numero,
        "agendamento_ativo",
        {"agendamento_id": agendamento.id, "servico_id": servico.id},
        ttl_segundos=_ttl_contexto(empresa),
    )

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=(
            f"Perfeito! Agendamento de *{servico.nome}* para *{formatar_data_hora(agendamento.data_hora)}* registrado ✅\n\n"
            f"O atendimento vai até *{agendamento.fim_em.strftime('%d/%m às %H:%M') if agendamento.fim_em else 'o horário previsto'}*.\n"
            f"Em breve alguém da {empresa.nome} confirma com você."
        ),
        botoes=[
            {"id": "agendamento:reagendar", "titulo": TEXTO_BOTAO_REAGENDAR},
            {"id": "agendamento:cancelar", "titulo": TEXTO_BOTAO_CANCELAR_AGENDAMENTO},
            {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
        ],
    )


async def _horario_texto_livre(db, empresa: Empresa, numero: str, texto: str, contexto: dict):
    servico = _obter_servico_por_contexto(db, contexto)
    if not servico:
        limpar_estado(empresa.id, numero)
        return

    inicio_em = parsear_data_hora_texto(texto)
    if inicio_em is None:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=(
                "Não consegui entender a data e a hora.\n"
                "Use o formato DD/MM/AAAA HH:MM ou DD/MM HH:MM."
            ),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    agendamento, validacao = agendar_servico(db, empresa, servico, numero, inicio_em)
    if not validacao.ok:
        if validacao.sugestoes:
            await enviar_lista(
                numero=_numero_limpo(numero),
                texto=validacao.mensagem,
                titulo_botao="Ver alternativas",
                secoes=_agrupa_slots_por_dia(validacao.sugestoes),
                rodape="Escolha um horário livre",
            )
        else:
            await enviar_botoes(
                numero=_numero_limpo(numero),
                texto=validacao.mensagem,
                botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
            )
        return

    salvar_estado(
        empresa.id,
        numero,
        "agendamento_ativo",
        {"agendamento_id": agendamento.id, "servico_id": servico.id},
        ttl_segundos=_ttl_contexto(empresa),
    )

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=(
            f"Perfeito! Agendamento de *{servico.nome}* para *{formatar_data_hora(agendamento.data_hora)}* registrado ✅\n\n"
            f"O atendimento vai até *{agendamento.fim_em.strftime('%d/%m às %H:%M') if agendamento.fim_em else 'o horário previsto'}*.\n"
            f"Em breve alguém da {empresa.nome} confirma com você."
        ),
        botoes=[
            {"id": "agendamento:reagendar", "titulo": TEXTO_BOTAO_REAGENDAR},
            {"id": "agendamento:cancelar", "titulo": TEXTO_BOTAO_CANCELAR_AGENDAMENTO},
            {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
        ],
    )


async def _mostrar_slots_reagendamento(db, empresa: Empresa, numero: str, contexto: dict, agendamento_id: int | None = None):
    agendamento = None
    if agendamento_id:
        agendamento = db.query(Agendamento).filter_by(id=agendamento_id, empresa_id=empresa.id).first()
    if not agendamento:
        agendamento = _agendamento_ativo_do_numero(db, empresa, numero)
    if not agendamento:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Não encontrei um agendamento ativo para reagendar.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    servico = agendamento.servico
    slots = obter_slots_disponiveis(
        db,
        empresa,
        servico,
        limite=LIMITE_SLOTS_EXIBIDOS,
        ignorar_agendamento_id=agendamento.id,
    )
    if not slots:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Não encontrei novos horários livres para reagendamento.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    salvar_estado(
        empresa.id,
        numero,
        "aguardando_reagendamento_slot",
        {"agendamento_id": agendamento.id, "servico_id": servico.id, "slots": [slot.id for slot in slots]},
        ttl_segundos=_ttl_contexto(empresa),
    )

    await enviar_lista(
        numero=_numero_limpo(numero),
        texto=(
            f"Escolha um novo horário para *{servico.nome}*:\n"
            f"Atual: {formatar_data_hora(agendamento.data_hora)}"
        ),
        titulo_botao="Ver novos horários",
        secoes=_agrupa_slots_por_dia(slots),
        rodape="Selecione o novo horário",
    )


async def _reagendar_slot_escolhido(db, empresa: Empresa, numero: str, texto: str, contexto: dict, id_interacao: str | None):
    agendamento_id = contexto.get("agendamento_id")
    if not agendamento_id:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Não encontrei o agendamento atual para reagendar.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    agendamento = db.query(Agendamento).filter_by(id=agendamento_id, empresa_id=empresa.id).first()
    if not agendamento:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Não encontrei o agendamento atual para reagendar.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    inicio_em = _id_slot_para_datetime(id_interacao) or parsear_data_hora_texto(texto)
    if inicio_em is None:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto="Não consegui entender o novo horário. Selecione um horário na lista ou digite no formato DD/MM/AAAA HH:MM.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    novo_agendamento, validacao = reagendar_agendamento(db, empresa, agendamento, inicio_em)
    if not validacao.ok:
        if validacao.sugestoes:
            salvar_estado(
                empresa.id,
                numero,
                "aguardando_reagendamento_slot",
                {"agendamento_id": agendamento.id, "servico_id": agendamento.servico_id, "slots": [slot.id for slot in validacao.sugestoes]},
                ttl_segundos=_ttl_contexto(empresa),
            )
            await enviar_lista(
                numero=_numero_limpo(numero),
                texto=validacao.mensagem,
                titulo_botao="Ver alternativas",
                secoes=_agrupa_slots_por_dia(validacao.sugestoes),
                rodape="Escolha um novo horário livre",
            )
        else:
            await enviar_botoes(
                numero=_numero_limpo(numero),
                texto=validacao.mensagem,
                botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
            )
        return

    salvar_estado(
        empresa.id,
        numero,
        "agendamento_ativo",
        {"agendamento_id": novo_agendamento.id, "servico_id": novo_agendamento.servico_id},
        ttl_segundos=_ttl_contexto(empresa),
    )

    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=(
            f"Agendamento reagendado com sucesso para *{formatar_data_hora(novo_agendamento.data_hora)}* ✅\n\n"
            f"Novo término previsto: *{novo_agendamento.fim_em.strftime('%d/%m às %H:%M') if novo_agendamento.fim_em else 'o horário previsto'}*."
        ),
        botoes=[
            {"id": "agendamento:reagendar", "titulo": TEXTO_BOTAO_REAGENDAR},
            {"id": "agendamento:cancelar", "titulo": TEXTO_BOTAO_CANCELAR_AGENDAMENTO},
            {"id": "menu", "titulo": TEXTO_BOTAO_MENU},
        ],
    )


async def _cancelar_agendamento_ativo(db, empresa: Empresa, numero: str):
    estado = obter_estado(empresa.id, numero)
    await _solicitar_confirmacao_cancelamento(db, empresa, numero, estado.get("contexto", {}))


async def _tentar_interpretar_via_ia(db, empresa: Empresa, numero: str, texto: str, contexto: dict) -> bool:
    """Último recurso quando a máquina de estados não determinou o próximo passo.

    Primeiro consulta a base de conhecimento da empresa (match determinístico, sem IA —
    garante que uma resposta cadastrada nunca seja substituída por algo inventado).
    Só se não achar nada relevante lá, cai na interpretação por IA. Em qualquer um dos
    dois casos, só mapeia para handlers que já existem e que não agem de forma destrutiva
    por conta própria (cancelar, por exemplo, sempre passa pela tela de confirmação).
    Se nada for acionável, devolve False e o fallback padrão de sempre continua.
    """
    entrada_conhecimento = buscar_resposta(db, empresa.id, texto)
    if entrada_conhecimento:
        await enviar_botoes(
            numero=_numero_limpo(numero),
            texto=entrada_conhecimento.resposta,
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return True

    config_sistema = obter_configuracao(db)
    servico_ia = criar_ai_service(config_sistema)
    interpretacao = await servico_ia.interpretar(empresa.id, texto)

    if interpretacao.intent == Intent.CANCELAR:
        await _cancelar_agendamento_ativo(db, empresa, numero)
        return True

    if interpretacao.intent == Intent.REAGENDAR:
        await _mostrar_slots_reagendamento(db, empresa, numero, contexto, contexto.get("agendamento_id"))
        return True

    if interpretacao.intent in {Intent.CONSULTAR_SERVICOS, Intent.CONSULTAR_PRECOS}:
        await _mostrar_lista_servicos(db, empresa, numero)
        return True

    if interpretacao.intent == Intent.FALAR_COM_ATENDENTE:
        await _mostrar_atendimento_humano(db, empresa, numero, texto, contexto)
        return True

    if interpretacao.intent == Intent.SAUDACAO:
        await _mostrar_menu_principal(db, empresa, numero, contexto)
        return True

    return False


async def processar_mensagem(db, empresa: Empresa, numero: str, texto: str, id_interacao: str | None):
    logger.debug("Mensagem recebida: texto=%r id_interacao=%r", texto, id_interacao)
    estado = obter_estado(empresa.id, numero)
    passo = estado["passo"]
    contexto = estado["contexto"]
    if passo == "novo":
        registrar_conversa_iniciada(db, empresa.id, _numero_limpo(numero))
    texto_lower = normalizar_texto(texto)
    logger.debug("Passo atual: %s | texto normalizado: %r", passo, texto_lower)

    if _texto_corresponde(texto, PALAVRAS_MENU) or id_interacao == "menu":
        limpar_estado(empresa.id, numero)
        await _mostrar_menu_principal(db, empresa, numero, {})
        return

    if id_interacao == "menu:servicos":
        limpar_estado(empresa.id, numero)
        await _mostrar_lista_servicos(db, empresa, numero)
        return

    if id_interacao == "atendimento:humano" or _texto_corresponde(texto, PALAVRAS_HUMANO):
        await _mostrar_atendimento_humano(db, empresa, numero, texto, contexto)
        return

    if passo == "aguardando_cancelamento_confirmacao":
        if id_interacao == "cancelamento:confirmar" or _texto_corresponde(texto, PALAVRAS_CONFIRMACAO):
            await _confirmar_cancelamento(db, empresa, numero, contexto)
        elif id_interacao == "cancelamento:manter" or _texto_corresponde(texto, PALAVRAS_NEGACAO):
            await _manter_agendamento(db, empresa, numero, contexto)
        else:
            texto_fallback, botoes_fallback = _resposta_fallback(passo)
            await enviar_botoes(numero=_numero_limpo(numero), texto=texto_fallback, botoes=botoes_fallback)
        return

    if id_interacao == "agendamento:cancelar" or _texto_corresponde(texto, PALAVRAS_CANCELAMENTO):
        await _cancelar_agendamento_ativo(db, empresa, numero)
        return

    if id_interacao == "agendamento:reagendar" or _texto_corresponde(texto, PALAVRAS_REAGENDAMENTO):
        await _mostrar_slots_reagendamento(db, empresa, numero, contexto, contexto.get("agendamento_id"))
        return

    if passo == "novo":
        palavras_abertura = parse_activation_words(empresa.palavra_ativacao) + SAUDACOES_FIXAS
        if _texto_corresponde(texto, palavras_abertura) or id_interacao in {"ver_servicos", "menu:servicos"}:
            await _mostrar_lista_servicos(db, empresa, numero)
        else:
            await _mostrar_menu_principal(db, empresa, numero, contexto)
        return

    if passo == "aguardando_servico":
        await _servico_escolhido(db, empresa, numero, texto, contexto, id_interacao)
        return

    if passo == "aguardando_periodo":
        await _periodo_escolhido(db, empresa, numero, texto, contexto, id_interacao)
        return

    if passo == "aguardando_slot":
        await _slot_escolhido(db, empresa, numero, texto, contexto, id_interacao)
        return

    if passo == "aguardando_horario_texto":
        await _horario_texto_livre(db, empresa, numero, texto, contexto)
        return

    if passo == "aguardando_reagendamento_slot":
        await _reagendar_slot_escolhido(db, empresa, numero, texto, contexto, id_interacao)
        return

    if passo == "aguardando_reagendamento_texto":
        await _reagendar_slot_escolhido(db, empresa, numero, texto, contexto, id_interacao)
        return

    if await _tentar_interpretar_via_ia(db, empresa, numero, texto, contexto):
        return

    texto_fallback, botoes_fallback = _resposta_fallback(passo)
    await enviar_botoes(
        numero=_numero_limpo(numero),
        texto=texto_fallback,
        botoes=botoes_fallback,
    )
