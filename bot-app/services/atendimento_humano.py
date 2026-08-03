from __future__ import annotations

from models import ClienteFinal, Empresa, SolicitacaoAtendimento

STATUS_PENDENTE = "pendente"
STATUS_EM_ATENDIMENTO = "em_atendimento"
STATUS_FINALIZADO = "finalizado"
STATUS_PERMITIDOS = {STATUS_PENDENTE, STATUS_EM_ATENDIMENTO, STATUS_FINALIZADO}


def _numero_limpo(numero: str) -> str:
    return numero.split("@")[0]


def _cliente_por_telefone(db, empresa_id: int, telefone: str):
    return (
        db.query(ClienteFinal)
        .filter_by(empresa_id=empresa_id, telefone=telefone)
        .order_by(ClienteFinal.id.desc())
        .first()
    )


def _solicitacao_pendente_por_telefone(db, empresa_id: int, telefone: str):
    return (
        db.query(SolicitacaoAtendimento)
        .filter_by(empresa_id=empresa_id, telefone=telefone, status=STATUS_PENDENTE)
        .order_by(SolicitacaoAtendimento.criado_em.desc(), SolicitacaoAtendimento.id.desc())
        .first()
    )


def registrar_solicitacao_atendimento(
    db,
    empresa: Empresa,
    telefone: str,
    mensagem: str,
    nome: str | None = None,
):
    telefone_limpo = _numero_limpo(telefone)
    solicitacao_existente = _solicitacao_pendente_por_telefone(db, empresa.id, telefone_limpo)
    if solicitacao_existente:
        return solicitacao_existente, False

    cliente = _cliente_por_telefone(db, empresa.id, telefone_limpo)
    nome_limpo = (nome or "").strip() or None
    if cliente is None:
        cliente = ClienteFinal(empresa_id=empresa.id, telefone=telefone_limpo, nome=nome_limpo)
        db.add(cliente)
        db.flush()
    elif nome_limpo and not cliente.nome:
        cliente.nome = nome_limpo

    solicitacao = SolicitacaoAtendimento(
        empresa_id=empresa.id,
        cliente_id=cliente.id if cliente else None,
        telefone=telefone_limpo,
        nome=nome_limpo or (cliente.nome if cliente else None),
        mensagem=(mensagem or "").strip() or "Solicitação de atendimento humano via WhatsApp",
        status=STATUS_PENDENTE,
    )
    db.add(solicitacao)
    db.commit()
    db.refresh(solicitacao)
    return solicitacao, True


def atualizar_status_solicitacao_atendimento(db, solicitacao: SolicitacaoAtendimento, status: str) -> SolicitacaoAtendimento:
    if status not in STATUS_PERMITIDOS:
        raise ValueError("Status inválido para solicitação de atendimento")

    solicitacao.status = status
    db.commit()
    db.refresh(solicitacao)
    return solicitacao