import importlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    admin_module = importlib.import_module("admin")
    models = importlib.import_module("core.models")
    usuarios = importlib.import_module("services.usuarios")
    main.ensure_schema()
    admin_module.enviar_email = AsyncMock(return_value=None)
    return main, admin_module, models, usuarios


def _criar_usuario(main, usuarios, empresa_id, email, senha="senha-antiga-123"):
    db = main.SessionLocal()
    try:
        return usuarios.criar_usuario(db, empresa_id=empresa_id, nome="Maria", email=email, senha=senha, papel="admin")
    finally:
        db.close()


def _seed_empresa(main, models, slug="clinica-sorriso-feliz"):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome="Clínica Sorriso Feliz",
            slug=slug,
            segmento="clinica",
            telefone_whatsapp="5586999999999",
            evolution_instance_name=slug,
            ativo=True,
        )
        db.add(empresa)
        db.commit()
        db.refresh(empresa)
        return empresa
    finally:
        db.close()


def test_solicitar_redefinicao_para_email_existente_gera_token_e_envia_email(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models)
    _criar_usuario(main, usuarios, empresa.id, "maria@clinica.com")

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/admin/esqueci-senha", data={"email": "maria@clinica.com"})

    assert resposta.status_code == 200
    assert "enviamos instruções" in resposta.text.lower()

    admin_module.enviar_email.assert_awaited_once()
    destinatario, assunto, corpo = admin_module.enviar_email.call_args.args
    assert destinatario == "maria@clinica.com"
    assert "/admin/redefinir-senha?token=" in corpo

    db = main.SessionLocal()
    try:
        usuario = db.query(models.UsuarioPainel).filter_by(email="maria@clinica.com").one()
    finally:
        db.close()
    assert usuario.reset_token_hash is not None
    assert usuario.reset_token_expira_em is not None


def test_solicitar_redefinicao_para_email_inexistente_nao_vaza_informacao(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/admin/esqueci-senha", data={"email": "ninguem@clinica.com"})

    assert resposta.status_code == 200
    assert "enviamos instruções" in resposta.text.lower()
    admin_module.enviar_email.assert_not_awaited()


def test_redefinir_senha_com_token_valido_permite_login_com_nova_senha(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models)
    _criar_usuario(main, usuarios, empresa.id, "maria@clinica.com")

    db = main.SessionLocal()
    try:
        token = usuarios.solicitar_redefinicao_senha(db, "maria@clinica.com")
    finally:
        db.close()

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            "/admin/redefinir-senha",
            data={"token": token, "senha": "senha-nova-123"},
            follow_redirects=False,
        )
        assert resposta.status_code == 303
        assert "redefinida" in resposta.headers["location"]

        login_antigo = client.post(
            "/admin/login", data={"username": "maria@clinica.com", "password": "senha-antiga-123"}, follow_redirects=False
        )
        assert login_antigo.status_code == 401

        login_novo = client.post(
            "/admin/login", data={"username": "maria@clinica.com", "password": "senha-nova-123"}, follow_redirects=False
        )
        assert login_novo.status_code == 303
        assert login_novo.headers["location"] == "/admin/dashboard"

    db = main.SessionLocal()
    try:
        usuario = db.query(models.UsuarioPainel).filter_by(email="maria@clinica.com").one()
    finally:
        db.close()
    assert usuario.reset_token_hash is None
    assert usuario.reset_token_expira_em is None


def test_redefinir_senha_com_token_invalido_e_rejeitado(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models)
    _criar_usuario(main, usuarios, empresa.id, "maria@clinica.com")

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            "/admin/redefinir-senha",
            data={"token": "token-que-nao-existe", "senha": "senha-nova-123"},
        )

    assert resposta.status_code == 400
    assert "inválido ou expirado" in resposta.text.lower()


def test_redefinir_senha_com_token_expirado_e_rejeitado(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models)
    _criar_usuario(main, usuarios, empresa.id, "maria@clinica.com")

    db = main.SessionLocal()
    try:
        token = usuarios.solicitar_redefinicao_senha(db, "maria@clinica.com")
        usuario = db.query(models.UsuarioPainel).filter_by(email="maria@clinica.com").one()
        usuario.reset_token_expira_em = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            "/admin/redefinir-senha",
            data={"token": token, "senha": "senha-nova-123"},
        )

    assert resposta.status_code == 400
    assert "inválido ou expirado" in resposta.text.lower()


def test_redefinir_senha_rejeita_senha_curta(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models)
    _criar_usuario(main, usuarios, empresa.id, "maria@clinica.com")

    db = main.SessionLocal()
    try:
        token = usuarios.solicitar_redefinicao_senha(db, "maria@clinica.com")
    finally:
        db.close()

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/admin/redefinir-senha", data={"token": token, "senha": "123"})

    assert resposta.status_code == 400
    assert "pelo menos 8 caracteres" in resposta.text
