from __future__ import annotations

import json
from datetime import datetime

from agenda import (
    agendar_servico,
    cancelar_agendamento,
    formatar_data_hora,
    obter_slots_disponiveis,
    parsear_data_hora_texto,
    reagendar_agendamento,
)
from config import settings
from meta_client import enviar_botoes, enviar_lista
from models import Agendamento, ClienteFinal, Empresa, Servico
from redis_client import redis_cliente

TEMPO_EXPIRACAO_SEGUNDOS = 30 * 60
PALAVRAS_ATIVACAO = settings.bot_activation_words
LIMITE_SLOTS_EXIBIDOS = 10

TEXTO_BOTAO_MANHA = "Manhã"
TEXTO_BOTAO_TARDE = "Tarde"
TEXTO_BOTAO_OUTRO = "Prefiro digitar"
TEXTO_BOTAO_VER_SERVICOS = "Ver serviços de novo"
TEXTO_BOTAO_MENU = "Menu"
TEXTO_BOTAO_REAGENDAR = "Reagendar"
TEXTO_BOTAO_CANCELAR_AGENDAMENTO = "Cancelar agendamento"


def _chave_estado(empresa_id: int, numero: str) -> str:
    return f"conversa:{empresa_id}:{numero}"


def obter_estado(empresa_id: int, numero: str) -> dict:
    bruto = redis_cliente.get(_chave_estado(empresa_id, numero))
    if bruto:
        return json.loads(bruto)
    return {"passo": "novo", "contexto": {}}


def salvar_estado(empresa_id: int, numero: str, passo: str, contexto: dict):
    dado = json.dumps({"passo": passo, "contexto": contexto})
    redis_cliente.set(_chave_estado(empresa_id, numero), dado, ex=TEMPO_EXPIRACAO_SEGUNDOS)


def limpar_estado(empresa_id: int, numero: str):
    redis_cliente.delete(_chave_estado(empresa_id, numero))


def _texto_bate(texto: str, alvo: str) -> bool:
    return texto.strip().lower() == alvo.strip().lower()


def _texto_id_bate(id_interacao: str | None, prefixo: str) -> bool:
    return bool(id_interacao and id_interacao.startswith(prefixo))


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
    return db.query(Servico).filter_by(id=servico_id).first()


def _agendamento_ativo_do_numero(db, empresa: Empresa, numero: str):
    numero_limpo = numero.split("@")[0]
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


async def _mostrar_lista_servicos(db, empresa: Empresa, numero: str):
    servicos = db.query(Servico).filter_by(empresa_id=empresa.id, ativo=True).all()
    if not servicos:
        await enviar_botoes(
            numero=numero.split("@")[0],
            texto=(
                f"Olá! No momento a {empresa.nome} não tem serviços ativos cadastrados.\n\n"
                "Peça para o administrador liberar a oferta antes de agendar."
            ),
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
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
        numero=numero.split("@")[0],
        texto=f"Olá! Bem-vindo(a) à {empresa.nome} 😊\n\nEscolha um serviço para agendar:",
        titulo_botao="Ver serviços",
        secoes=secoes,
        rodape="Toque em uma opção da lista",
    )

    salvar_estado(empresa.id, numero, "aguardando_servico", {"servicos_ids": [s.id for s in servicos]})


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
            numero=numero.split("@")[0],
            texto="Por favor, toque em uma das opções da lista para escolher o serviço.",
            botoes=[{"id": "ver_servicos", "titulo": TEXTO_BOTAO_VER_SERVICOS}],
        )
        return

    contexto = {"servico_id": servico.id}
    salvar_estado(empresa.id, numero, "aguardando_periodo", contexto)

    await enviar_botoes(
        numero=numero.split("@")[0],
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
        salvar_estado(empresa.id, numero, "aguardando_horario_texto", contexto)
        await enviar_botoes(
            numero=numero.split("@")[0],
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
            numero=numero.split("@")[0],
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
        salvar_estado(empresa.id, numero, "aguardando_periodo", contexto)
        await enviar_botoes(
            numero=numero.split("@")[0],
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
    )

    await enviar_lista(
        numero=numero.split("@")[0],
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
            numero=numero.split("@")[0],
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
            salvar_estado(empresa.id, numero, "aguardando_slot", contexto)
            await enviar_lista(
                numero=numero.split("@")[0],
                texto=validacao.mensagem,
                titulo_botao="Ver alternativas",
                secoes=_agrupa_slots_por_dia(validacao.sugestoes),
                rodape="Escolha um horário livre",
            )
        else:
            salvar_estado(empresa.id, numero, "aguardando_periodo", contexto)
            await enviar_botoes(
                numero=numero.split("@")[0],
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
            numero=numero.split("@")[0],
            texto="Não foi possível concluir o agendamento. Tente novamente em instantes.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    salvar_estado(
        empresa.id,
        numero,
        "agendamento_ativo",
        {"agendamento_id": agendamento.id, "servico_id": servico.id},
    )

    await enviar_botoes(
        numero=numero.split("@")[0],
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
            numero=numero.split("@")[0],
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
                numero=numero.split("@")[0],
                texto=validacao.mensagem,
                titulo_botao="Ver alternativas",
                secoes=_agrupa_slots_por_dia(validacao.sugestoes),
                rodape="Escolha um horário livre",
            )
        else:
            await enviar_botoes(
                numero=numero.split("@")[0],
                texto=validacao.mensagem,
                botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
            )
        return

    salvar_estado(
        empresa.id,
        numero,
        "agendamento_ativo",
        {"agendamento_id": agendamento.id, "servico_id": servico.id},
    )

    await enviar_botoes(
        numero=numero.split("@")[0],
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
            numero=numero.split("@")[0],
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
            numero=numero.split("@")[0],
            texto="Não encontrei novos horários livres para reagendamento.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    salvar_estado(
        empresa.id,
        numero,
        "aguardando_reagendamento_slot",
        {"agendamento_id": agendamento.id, "servico_id": servico.id, "slots": [slot.id for slot in slots]},
    )

    await enviar_lista(
        numero=numero.split("@")[0],
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
            numero=numero.split("@")[0],
            texto="Não encontrei o agendamento atual para reagendar.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    agendamento = db.query(Agendamento).filter_by(id=agendamento_id, empresa_id=empresa.id).first()
    if not agendamento:
        await enviar_botoes(
            numero=numero.split("@")[0],
            texto="Não encontrei o agendamento atual para reagendar.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    inicio_em = _id_slot_para_datetime(id_interacao) or parsear_data_hora_texto(texto)
    if inicio_em is None:
        await enviar_botoes(
            numero=numero.split("@")[0],
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
            )
            await enviar_lista(
                numero=numero.split("@")[0],
                texto=validacao.mensagem,
                titulo_botao="Ver alternativas",
                secoes=_agrupa_slots_por_dia(validacao.sugestoes),
                rodape="Escolha um novo horário livre",
            )
        else:
            await enviar_botoes(
                numero=numero.split("@")[0],
                texto=validacao.mensagem,
                botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
            )
        return

    salvar_estado(
        empresa.id,
        numero,
        "agendamento_ativo",
        {"agendamento_id": novo_agendamento.id, "servico_id": novo_agendamento.servico_id},
    )

    await enviar_botoes(
        numero=numero.split("@")[0],
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
    agendamento = _agendamento_ativo_do_numero(db, empresa, numero)
    if not agendamento:
        await enviar_botoes(
            numero=numero.split("@")[0],
            texto="Não encontrei um agendamento ativo para cancelar.",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
        )
        return

    cancelar_agendamento(db, agendamento)
    limpar_estado(empresa.id, numero)

    await enviar_botoes(
        numero=numero.split("@")[0],
        texto=(
            f"Agendamento de *{agendamento.servico.nome if agendamento.servico else 'serviço'}* em *{formatar_data_hora(agendamento.data_hora)}* cancelado."
        ),
        botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_MENU}],
    )


async def processar_mensagem(db, empresa: Empresa, numero: str, texto: str, id_interacao: str | None):
    print(f"[DEBUG] texto recebido: {texto!r} | id_interacao: {id_interacao!r}")
    estado = obter_estado(empresa.id, numero)
    passo = estado["passo"]
    contexto = estado["contexto"]
    texto_lower = texto.strip().lower()
    print(f"[DEBUG] passo atual: {passo} | texto_lower: {texto_lower!r}")

    if texto_lower in ("menu", "voltar") or id_interacao == "menu":
        limpar_estado(empresa.id, numero)
        passo, contexto = "novo", {}

    if id_interacao == "agendamento:cancelar" or texto_lower == "cancelar agendamento":
        await _cancelar_agendamento_ativo(db, empresa, numero)
        return

    if id_interacao == "agendamento:reagendar" or texto_lower in ("reagendar", "remarcar"):
        await _mostrar_slots_reagendamento(db, empresa, numero, contexto, contexto.get("agendamento_id"))
        return

    if passo == "novo":
        if texto_lower in PALAVRAS_ATIVACAO or id_interacao == "ver_servicos":
            await _mostrar_lista_servicos(db, empresa, numero)
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

    limpar_estado(empresa.id, numero)
