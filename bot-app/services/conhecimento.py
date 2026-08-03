from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import joinedload

from core.models import EmpresaConhecimento

from .texto_utils import normalizar_texto

LIMIAR_CONFIANCA = 0.6
TAMANHO_PREFIXO = 5
TAMANHO_MINIMO_PALAVRA = 3

STOPWORDS = {
    "a", "as", "ao", "aos", "com", "como", "da", "das", "de", "do", "dos",
    "e", "em", "essa", "esse", "esta", "este", "eu", "isso", "isto",
    "la", "lhe", "meu", "minha", "na", "no", "nos", "nas", "num", "numa",
    "o", "os", "ou", "para", "pelo", "pela", "por", "que", "se", "sem",
    "seu", "sua", "tem", "ter", "um", "uma", "uns", "voce", "voces", "vou",
}


def criar_conhecimento(db, empresa_id: int, categoria: str | None, pergunta: str, resposta: str, ativo: bool = True) -> EmpresaConhecimento:
    entrada = EmpresaConhecimento(
        empresa_id=empresa_id,
        categoria=categoria,
        pergunta=pergunta,
        resposta=resposta,
        ativo=ativo,
    )
    db.add(entrada)
    db.commit()
    db.refresh(entrada)
    return entrada


def atualizar_conhecimento(entrada: EmpresaConhecimento, categoria: str | None, pergunta: str, resposta: str, ativo: bool) -> None:
    entrada.categoria = categoria
    entrada.pergunta = pergunta
    entrada.resposta = resposta
    entrada.ativo = ativo
    entrada.atualizado_em = datetime.utcnow()


def listar_conhecimento(db, empresa_id: int | None, incluir_inativos: bool = True):
    query = (
        db.query(EmpresaConhecimento)
        .options(joinedload(EmpresaConhecimento.empresa))
        .filter(EmpresaConhecimento.excluido_em.is_(None))
    )
    if empresa_id:
        query = query.filter(EmpresaConhecimento.empresa_id == empresa_id)
    if not incluir_inativos:
        query = query.filter(EmpresaConhecimento.ativo.is_(True))
    return query.order_by(EmpresaConhecimento.categoria.asc(), EmpresaConhecimento.pergunta.asc()).all()


def excluir_conhecimento(entrada: EmpresaConhecimento) -> None:
    entrada.ativo = False
    entrada.excluido_em = datetime.utcnow()


def _palavras_significativas(texto: str) -> set[str]:
    normalizado = normalizar_texto(texto)
    palavras = re.findall(r"[a-z0-9]+", normalizado)
    return {p[:TAMANHO_PREFIXO] for p in palavras if p not in STOPWORDS and len(p) >= TAMANHO_MINIMO_PALAVRA}


def buscar_resposta(db, empresa_id: int, texto_cliente: str) -> EmpresaConhecimento | None:
    """
    Compara a mensagem do cliente com as perguntas cadastradas ativas da empresa.

    Matching é uma heurística simples (prefixo de palavra + proporção de sobreposição),
    não busca semântica/embeddings — suficiente para perguntas curtas e diretas como
    "aceita Unimed?"/"tem estacionamento?". Só retorna uma entrada quando a pontuação
    bate o limiar, para nunca "forçar" uma resposta errada.
    """
    palavras_cliente = _palavras_significativas(texto_cliente)
    if not palavras_cliente:
        return None

    entradas = (
        db.query(EmpresaConhecimento)
        .filter(
            EmpresaConhecimento.empresa_id == empresa_id,
            EmpresaConhecimento.ativo.is_(True),
            EmpresaConhecimento.excluido_em.is_(None),
        )
        .all()
    )

    melhor_entrada = None
    melhor_pontuacao = 0.0
    for entrada in entradas:
        palavras_pergunta = _palavras_significativas(entrada.pergunta)
        if not palavras_pergunta:
            continue
        intersecao = palavras_cliente & palavras_pergunta
        pontuacao = len(intersecao) / len(palavras_pergunta)
        if pontuacao > melhor_pontuacao:
            melhor_pontuacao = pontuacao
            melhor_entrada = entrada

    if melhor_entrada and melhor_pontuacao >= LIMIAR_CONFIANCA:
        return melhor_entrada
    return None
