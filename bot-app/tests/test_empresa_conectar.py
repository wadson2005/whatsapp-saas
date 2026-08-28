import importlib
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    usuarios = importlib.import_module("services.usuarios")
    main.ensure_schema()
    return main, models, usuarios


def _seed_empresa(main, models, slug: str, nome: str):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome=nome,
            slug=slug,
            segmento="clinica",
            telefone_whatsapp=f"5511999{sum(ord(c) for c in slug) % 1000000:06d}",
            evolution_instance_name=slug,
            horario_abertura="08:00",
            horario_fechamento="18:00",
            intervalo_entre_atendimentos_minutos=15,
            ativo=True,
        )
        db.add(empresa)
        db.commit()
        db.refresh(empresa)
        return empresa
    finally:
        db.close()


def _criar_usuario(main, usuarios, empresa, nome, email, senha, papel):
    db = main.SessionLocal()
    try:
        return usuarios.criar_usuario(db, empresa_id=empresa.id, nome=nome, email=email, senha=senha, papel=papel)
    finally:
        db.close()


def _login_superadmin(client: TestClient):
    resposta = client.post(
        "/admin/login",
        data={"username": "admin", "password": "senha-super-segura-123"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303


def _login(client: TestClient, email: str, senha: str):
    return client.post("/admin/login", data={"username": email, "password": senha}, follow_redirects=False)


def _mockar_evolution(admin_module, *, qrcode=None, estado="close", falhar=False):
    from integrations.evolution_client import EvolutionAPIError

    if falhar:
        admin_module.gerar_qrcode = AsyncMock(side_effect=EvolutionAPIError("Evolution API indisponível"))
    else:
        admin_module.gerar_qrcode = AsyncMock(return_value=qrcode or {"pairingCode": "WZYEH1YY", "code": "2@abc", "count": 1})
    admin_module.estado_conexao = AsyncMock(return_value=estado)


def test_superadmin_ve_qrcode_de_qualquer_empresa(monkeypatch, tmp_path):
    main, models, _ = carregar_app(monkeypatch, tmp_path)
    admin_module = importlib.import_module("admin")
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)

        resposta = client.get(f"/admin/empresas/{empresa.id}/conectar")
        assert resposta.status_code == 200
        assert "WZYEH1YY" in resposta.text


def test_operador_nao_acessa_reconectar(monkeypatch, tmp_path):
    main, models, usuarios = carregar_app(monkeypatch, tmp_path)
    admin_module = importlib.import_module("admin")
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _criar_usuario(main, usuarios, empresa, "Bruno", "bruno@clinica-a.com", "senha12345", usuarios.PAPEL_OPERADOR)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "bruno@clinica-a.com", "senha12345")

        assert client.get(f"/admin/empresas/{empresa.id}/conectar").status_code == 403


def test_admin_de_outra_empresa_nao_acessa_reconectar(monkeypatch, tmp_path):
    main, models, usuarios = carregar_app(monkeypatch, tmp_path)
    admin_module = importlib.import_module("admin")
    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B")
    _criar_usuario(main, usuarios, empresa_b, "Carla", "carla@clinica-b.com", "senha12345", usuarios.PAPEL_ADMIN)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "carla@clinica-b.com", "senha12345")

        assert client.get(f"/admin/empresas/{empresa_a.id}/conectar").status_code == 404


def test_admin_da_propria_empresa_conecta_e_verifica_status(monkeypatch, tmp_path):
    main, models, usuarios = carregar_app(monkeypatch, tmp_path)
    admin_module = importlib.import_module("admin")
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _criar_usuario(main, usuarios, empresa, "Carla", "carla@clinica-a.com", "senha12345", usuarios.PAPEL_ADMIN)
    _mockar_evolution(admin_module, estado="open")

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "carla@clinica-a.com", "senha12345")

        assert client.get(f"/admin/empresas/{empresa.id}/conectar").status_code == 200

        status = client.get(f"/admin/empresas/{empresa.id}/conectar/status")
        assert status.status_code == 200
        assert status.json() == {"state": "open"}

        novo = client.post(f"/admin/empresas/{empresa.id}/conectar/novo-qrcode")
        assert novo.status_code == 200
        assert novo.json()["pairingCode"] == "WZYEH1YY"


def test_pagina_de_conectar_degrada_com_erro_quando_evolution_falha(monkeypatch, tmp_path):
    main, models, _ = carregar_app(monkeypatch, tmp_path)
    admin_module = importlib.import_module("admin")
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _mockar_evolution(admin_module, falhar=True)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)

        resposta = client.get(f"/admin/empresas/{empresa.id}/conectar")
        assert resposta.status_code == 200
        assert "Não foi possível gerar o código de conexão" in resposta.text
