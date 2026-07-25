import json
from datetime import datetime
from redis_client import redis_cliente
from models import Empresa, Servico, ClienteFinal, Agendamento

TEMPO_EXPIRACAO_SEGUNDOS = 30 * 60  # conversa "esquecida" após 30 min de silêncio


def _chave_estado(empresa_id: int, numero: str) -> str:
    return f"conversa:{empresa_id}:{numero}"


def obter_estado(empresa_id: int, numero: str) -> dict:
    bruto = redis_cliente.get(_chave_estado(empresa_id, numero))
    if bruto:
        return json.loads(bruto)
    return {"passo": "novo", "contexto": {}}


def salvar_estado(empresa_id: int, numero: str, passo: str, contexto: dict):
    dado = json.dumps({"passo": passo, "contexto": contexto})
    redis_cliente.set(
        _chave_estado(empresa_id, numero),
        dado,
        ex=TEMPO_EXPIRACAO_SEGUNDOS,
    )


def limpar_estado(empresa_id: int, numero: str):
    redis_cliente.delete(_chave_estado(empresa_id, numero))


def processar_mensagem(db, empresa: Empresa, numero: str, texto: str) -> str:
    estado = obter_estado(empresa.id, numero)
    passo = estado["passo"]
    contexto = estado["contexto"]

    texto_lower = texto.strip().lower()

    # Reinício manual: a pessoa pode digitar "menu" a qualquer momento
    if texto_lower in ("menu", "voltar", "cancelar"):
        limpar_estado(empresa.id, numero)
        passo = "novo"
        contexto = {}

    if passo == "novo":
        return _iniciar_conversa(db, empresa, numero)

    if passo == "aguardando_servico":
        return _escolher_servico(db, empresa, numero, texto, contexto)

    if passo == "aguardando_horario":
        return _confirmar_horario(db, empresa, numero, texto, contexto)

    # segurança: qualquer estado desconhecido volta ao início
    limpar_estado(empresa.id, numero)
    return _iniciar_conversa(db, empresa, numero)


def _iniciar_conversa(db, empresa: Empresa, numero: str) -> str:
    servicos = db.query(Servico).filter_by(empresa_id=empresa.id, ativo=True).all()

    linhas = [f"Olá! Bem-vindo(a) à {empresa.nome} 😊", "", "Nossos serviços:"]
    for i, s in enumerate(servicos, start=1):
        linhas.append(f"{i}. {s.nome} — R$ {s.preco:.2f}")
    linhas.append("")
    linhas.append("Digite o número do serviço que deseja agendar.")

    contexto = {"servicos_ids": [s.id for s in servicos]}
    salvar_estado(empresa.id, numero, "aguardando_servico", contexto)

    return "\n".join(linhas)


def _escolher_servico(db, empresa: Empresa, numero: str, texto: str, contexto: dict) -> str:
    servicos_ids = contexto.get("servicos_ids", [])

    if not texto.strip().isdigit():
        return "Por favor, digite apenas o número do serviço desejado."

    indice = int(texto.strip()) - 1
    if indice < 0 or indice >= len(servicos_ids):
        return "Número inválido. Digite o número correspondente a um dos serviços listados."

    servico_id = servicos_ids[indice]
    servico = db.query(Servico).filter_by(id=servico_id).first()

    contexto["servico_id"] = servico_id
    salvar_estado(empresa.id, numero, "aguardando_horario", contexto)

    return (
        f"Você escolheu: {servico.nome}.\n\n"
        f"Agora me diga o dia e horário desejado (exemplo: 28/07 às 14h)."
    )


def _confirmar_horario(db, empresa: Empresa, numero: str, texto: str, contexto: dict) -> str:
    servico_id = contexto.get("servico_id")
    servico = db.query(Servico).filter_by(id=servico_id).first()

    numero_limpo = numero.split("@")[0]

    cliente = db.query(ClienteFinal).filter_by(
        empresa_id=empresa.id, telefone=numero_limpo
    ).first()
    if not cliente:
        cliente = ClienteFinal(empresa_id=empresa.id, telefone=numero_limpo)
        db.add(cliente)
        db.commit()
        db.refresh(cliente)

    agendamento = Agendamento(
        empresa_id=empresa.id,
        cliente_final_id=cliente.id,
        servico_id=servico.id,
        data_hora=datetime.utcnow(),  # por enquanto guardamos a data de agora;
                                       # ainda vamos interpretar o texto do horário depois
        status="pendente",
    )
    db.add(agendamento)
    db.commit()

    limpar_estado(empresa.id, numero)

    return (
        f"Perfeito! Seu agendamento de {servico.nome} para \"{texto}\" foi registrado ✅\n\n"
        f"Em breve alguém da {empresa.nome} vai confirmar com você."
    )