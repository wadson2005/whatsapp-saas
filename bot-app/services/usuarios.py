from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from core.models import UsuarioPainel
from core.security import hash_senha, verificar_senha

PAPEL_ADMIN = "admin"
PAPEL_OPERADOR = "operador"
PAPEIS_PERMITIDOS = {PAPEL_ADMIN, PAPEL_OPERADOR}

RESET_TOKEN_TTL_MINUTOS = 60


def autenticar_usuario(db, email: str, senha: str) -> UsuarioPainel | None:
    usuario = db.query(UsuarioPainel).filter_by(email=email, ativo=True).first()
    if usuario and verificar_senha(senha, usuario.senha_hash):
        return usuario
    return None


def listar_usuarios(db, empresa_id: int | None):
    query = db.query(UsuarioPainel).order_by(UsuarioPainel.nome.asc())
    if empresa_id:
        query = query.filter(UsuarioPainel.empresa_id == empresa_id)
    return query.all()


def criar_usuario(db, empresa_id: int, nome: str, email: str, senha: str, papel: str) -> UsuarioPainel:
    if papel not in PAPEIS_PERMITIDOS:
        raise ValueError("Papel inválido")
    usuario = UsuarioPainel(
        empresa_id=empresa_id,
        nome=nome,
        email=email,
        senha_hash=hash_senha(senha),
        papel=papel,
        ativo=True,
    )
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    return usuario


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def solicitar_redefinicao_senha(db, email: str) -> str | None:
    """Gera um token de redefinição para o e-mail informado, se houver usuário ativo com ele.

    Retorna o token em texto puro (só existe aqui — o banco guarda apenas o hash) ou
    `None` se não houver usuário correspondente. O chamador deve responder de forma
    idêntica nos dois casos, para não revelar por e-mail se ele tem cadastro ou não.
    """
    usuario = db.query(UsuarioPainel).filter_by(email=email, ativo=True).first()
    if not usuario:
        return None
    token = secrets.token_urlsafe(32)
    usuario.reset_token_hash = _hash_token(token)
    usuario.reset_token_expira_em = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTOS)
    db.commit()
    return token


def redefinir_senha(db, token: str, nova_senha: str) -> UsuarioPainel | None:
    """Troca a senha se o token existir e ainda não tiver expirado. Retorna o usuário ou `None`."""
    usuario = db.query(UsuarioPainel).filter_by(reset_token_hash=_hash_token(token), ativo=True).first()
    if not usuario or not usuario.reset_token_expira_em or usuario.reset_token_expira_em < datetime.utcnow():
        return None
    usuario.senha_hash = hash_senha(nova_senha)
    usuario.reset_token_hash = None
    usuario.reset_token_expira_em = None
    db.commit()
    return usuario


def atualizar_usuario(
    db,
    usuario: UsuarioPainel,
    nome: str,
    papel: str,
    ativo: bool,
    nova_senha: str | None = None,
) -> UsuarioPainel:
    if papel not in PAPEIS_PERMITIDOS:
        raise ValueError("Papel inválido")
    usuario.nome = nome
    usuario.papel = papel
    usuario.ativo = ativo
    if nova_senha:
        usuario.senha_hash = hash_senha(nova_senha)
    db.commit()
    db.refresh(usuario)
    return usuario
