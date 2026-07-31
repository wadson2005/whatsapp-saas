import json
from datetime import datetime
from redis_client import redis_cliente
from models import Empresa, Servico, ClienteFinal, Agendamento
from meta_client import enviar_botoes, enviar_lista

TEMPO_EXPIRACAO_SEGUNDOS = 30 * 60
PALAVRAS_ATIVACAO = ("oibot",)  # ajuste como preferir

# Textos dos botões de período — usados tanto para EXIBIR quanto para RECONHECER
# a escolha depois, já que a Evolution API devolve apenas o texto visível do botão,
# sem o id original que enviamos (limitação confirmada na prática).
TEXTO_BOTAO_MANHA = "Manhã"
TEXTO_BOTAO_TARDE = "Tarde"
TEXTO_BOTAO_OUTRO = "Prefiro digitar"
TEXTO_BOTAO_VER_SERVICOS = "Ver serviços de novo"
TEXTO_BOTAO_CANCELAR = "Cancelar"
TEXTO_BOTAO_NOVO_AGENDAMENTO = "Fazer novo agendamento"


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
    """Compara ignorando maiúsculas/minúsculas e espaços nas pontas —
    é assim que comparamos o texto que volta de um toque em botão/lista,
    já que a Evolution API não repassa o id original de forma confiável."""
    return texto.strip().lower() == alvo.strip().lower()


async def processar_mensagem(db, empresa: Empresa, numero: str, texto: str, id_interacao: str | None):
    print(f"[DEBUG] texto recebido: {repr(texto)} | id_interacao: {repr(id_interacao)}")
    estado = obter_estado(empresa.id, numero)
    passo = estado["passo"]
    contexto = estado["contexto"]
    texto_lower = texto.strip().lower()
    print(f"[DEBUG] passo atual: {passo} | texto_lower: {repr(texto_lower)}")

    # "menu"/"voltar" reinicia a qualquer momento, seja por texto digitado ou toque em botão
    if texto_lower in ("menu", "voltar", "cancelar") or id_interacao == "menu":
        limpar_estado(empresa.id, numero)
        passo, contexto = "novo", {}

    if passo == "novo":
        if texto_lower in PALAVRAS_ATIVACAO or id_interacao == "ver_servicos":
            await _mostrar_lista_servicos(db, empresa, numero)
        # qualquer outra mensagem, nesse estado, é ignorada (filtro de ativação)
        return

    if passo == "aguardando_servico":
        await _servico_escolhido(db, empresa, numero, texto, contexto)
        return

    if passo == "aguardando_horario":
        await _horario_escolhido(db, empresa, numero, texto, contexto)
        return

    if passo == "aguardando_horario_texto":
        await _horario_texto_livre(db, empresa, numero, texto, contexto)
        return

    # estado desconhecido: reinicia com segurança
    limpar_estado(empresa.id, numero)


async def _mostrar_lista_servicos(db, empresa: Empresa, numero: str):
    servicos = db.query(Servico).filter_by(empresa_id=empresa.id, ativo=True).all()

    if not servicos:
        # sem serviço cadastrado: não há o que listar — evita mandar uma lista vazia
        return

    secoes = [{
        "titulo": "Serviços disponíveis",
        "linhas": [
            {"id": f"servico:{s.id}", "titulo": s.nome, "descricao": f"R$ {s.preco:.2f} · {s.duracao_minutos} min"}
            for s in servicos
        ],
    }]

    await enviar_lista(
        numero=numero.split("@")[0],
        texto=f"Olá! Bem-vindo(a) à {empresa.nome} 😊\n\nEscolha um serviço para agendar:",
        titulo_botao="Ver serviços",
        secoes=secoes,
        rodape="Toque em uma opção da lista",
    )

    # Guardamos os IDs dos serviços que aparecem NESSA lista específica —
    # é contra essa lista que vamos comparar o texto/nome escolhido depois,
    # já que não podemos confiar no id devolvido pelo clique.
    ids_servicos = [s.id for s in servicos]
    salvar_estado(empresa.id, numero, "aguardando_servico", {"servicos_ids": ids_servicos})


async def _servico_escolhido(db, empresa: Empresa, numero: str, texto: str, contexto: dict):
    servicos_ids = contexto.get("servicos_ids", [])
    servico = None

    if servicos_ids:
        candidatos = db.query(Servico).filter(Servico.id.in_(servicos_ids)).all()
        for s in candidatos:
            if _texto_bate(texto, s.nome):
                servico = s
                break

    if not servico:
        # a pessoa digitou algo que não bate com nenhum serviço da lista mostrada
        await enviar_botoes(
            numero=numero.split("@")[0],
            texto="Por favor, toque em uma das opções da lista para escolher o serviço.",
            botoes=[{"id": "ver_servicos", "titulo": TEXTO_BOTAO_VER_SERVICOS}],
        )
        return

    contexto["servico_id"] = servico.id
    salvar_estado(empresa.id, numero, "aguardando_horario", contexto)

    await enviar_botoes(
        numero=numero.split("@")[0],
        texto=f"Você escolheu: {servico.nome}.\n\nQual período prefere?",
        botoes=[
            {"id": "periodo:manha", "titulo": TEXTO_BOTAO_MANHA},
            {"id": "periodo:tarde", "titulo": TEXTO_BOTAO_TARDE},
            {"id": "periodo:outro", "titulo": TEXTO_BOTAO_OUTRO},
        ],
        rodape="Depois confirmamos o horário exato",
    )


async def _horario_escolhido(db, empresa: Empresa, numero: str, texto: str, contexto: dict):
    servico_id = contexto.get("servico_id")
    servico = db.query(Servico).filter_by(id=servico_id).first()
    if not servico:
        limpar_estado(empresa.id, numero)
        return

    if _texto_bate(texto, TEXTO_BOTAO_OUTRO):
        # pede pra pessoa digitar livremente — muda de "botão" pra "texto" nesse ponto específico
        salvar_estado(empresa.id, numero, "aguardando_horario_texto", contexto)
        await enviar_botoes(
            numero=numero.split("@")[0],
            texto="Sem problemas! Me diga o dia e horário que prefere (ex: 29/07 às 15h).",
            botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_CANCELAR}],
        )
        return

    periodo_escolhido = None
    if _texto_bate(texto, TEXTO_BOTAO_MANHA):
        periodo_escolhido = "período da manhã"
    elif _texto_bate(texto, TEXTO_BOTAO_TARDE):
        periodo_escolhido = "período da tarde"

    if not periodo_escolhido:
        # não reconhecido: pede pra tocar em uma das opções de novo
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

    await _confirmar_agendamento(db, empresa, numero, servico, periodo_escolhido)


async def _horario_texto_livre(db, empresa: Empresa, numero: str, texto: str, contexto: dict):
    servico_id = contexto.get("servico_id")
    servico = db.query(Servico).filter_by(id=servico_id).first()
    if not servico:
        limpar_estado(empresa.id, numero)
        return

    if not texto.strip():
        return

    await _confirmar_agendamento(db, empresa, numero, servico, texto.strip())


async def _confirmar_agendamento(db, empresa: Empresa, numero: str, servico: Servico, quando_texto: str):
    numero_limpo = numero.split("@")[0]

    cliente = db.query(ClienteFinal).filter_by(empresa_id=empresa.id, telefone=numero_limpo).first()
    if not cliente:
        cliente = ClienteFinal(empresa_id=empresa.id, telefone=numero_limpo)
        db.add(cliente)
        db.commit()
        db.refresh(cliente)

    agendamento = Agendamento(
        empresa_id=empresa.id,
        cliente_final_id=cliente.id,
        servico_id=servico.id,
        data_hora=datetime.utcnow(),  # ainda texto livre por trás — data real vem na próxima etapa (IA)
        status="pendente",
    )
    db.add(agendamento)
    db.commit()

    limpar_estado(empresa.id, numero)

    await enviar_botoes(
        numero=numero_limpo,
        texto=(
            f"Perfeito! Agendamento de *{servico.nome}* para *{quando_texto}* registrado ✅\n\n"
            f"Em breve alguém da {empresa.nome} confirma com você."
        ),
        botoes=[{"id": "menu", "titulo": TEXTO_BOTAO_NOVO_AGENDAMENTO}],
    )
