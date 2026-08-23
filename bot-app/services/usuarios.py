from __future__ import annotations

from core.models import UsuarioPainel
from core.security import hash_senha, verificar_senha

PAPEL_ADMIN = "admin"
PAPEL_OPERADOR = "operador"
PAPEIS_PERMITIDOS = {PAPEL_ADMIN, PAPEL_OPERADOR}


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
