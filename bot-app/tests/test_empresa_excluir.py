import importlib
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
    admin_module.excluir_instancia = AsyncMock(return_value=None)
    return main, admin_module, models, usuarios


def _seed_empresa(main, models, slug: str = "clinica-a", nome: str = "Clínica A"):
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


def _seed_servico(main, models, empresa, nome: str = "Corte"):
    db = main.SessionLocal()
    try:
        servico = models.Servico(empresa_id=empresa.id, nome=nome, duracao_minutos=30, ativo=True)
        db.add(servico)
        db.commit()
        db.refresh(servico)
        return servico
    finally:
        db.close()


def _seed_cliente(main, models, empresa, telefone: str = "5511900000001", nome: str = "Ana"):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome)
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
        return cliente
    finally:
        db.close()


def _seed_agendamento(main, models, empresa, cliente, servico):
    from datetime import datetime, timedelta

    db = main.SessionLocal()
    try:
        agendamento = models.Agendamento(
            empresa_id=empresa.id,
            cliente_final_id=cliente.id,
            servico_id=servico.id,
            data_hora=datetime.utcnow() + timedelta(hours=2),
            duracao_minutos=servico.duracao_minutos,
            status="agendado",
        )
        db.add(agendamento)
        db.commit()
        db.refresh(agendamento)
        return agendamento
    finally:
        db.close()


def _criar_usuario(main, usuarios, empresa, nome: str, email: str, senha: str, papel: str):
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


def test_superadmin_exclui_empresa_e_apaga_dados_vinculados(monkeypatch, tmp_path):
    main, admin_module, models, _ = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models)
    servico = _seed_servico(main, models, empresa)
    cliente = _seed_cliente(main, models, empresa)
    _seed_agendamento(main, models, empresa, cliente, servico)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)
        resposta = client.post(f"/admin/empresas/{empresa.id}/excluir", follow_redirects=False)

    assert resposta.status_code == 303
    assert resposta.headers["location"].startswith("/admin/empresas?message=")
    admin_module.excluir_instancia.assert_awaited_once_with("clinica-a")

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
        assert db.query(models.Servico).count() == 0
        assert db.query(models.ClienteFinal).count() == 0
        assert db.query(models.Agendamento).count() == 0
    finally:
        db.close()


def test_excluir_empresa_desvincula_usuarios_em_vez_de_apagar(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models)
    usuario_admin = _criar_usuario(main, usuarios, empresa, "Dona", "dona@exemplo.com", "senha-super-segura", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)
        resposta = client.post(f"/admin/empresas/{empresa.id}/excluir", follow_redirects=False)

    assert resposta.status_code == 303

    db = main.SessionLocal()
    try:
        atualizado = db.query(models.UsuarioPainel).filter_by(id=usuario_admin.id).first()
        assert atualizado is not None
        assert atualizado.empresa_id is None
    finally:
        db.close()


def test_operador_nao_pode_excluir_empresa(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models)
    _criar_usuario(main, usuarios, empresa, "Recepção", "recepcao@exemplo.com", "senha-super-segura", usuarios.PAPEL_OPERADOR)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "recepcao@exemplo.com", "senha-super-segura")
        resposta = client.post(f"/admin/empresas/{empresa.id}/excluir", follow_redirects=False)

    assert resposta.status_code == 403

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 1
    finally:
        db.close()


def test_admin_da_empresa_exclui_a_propria_empresa_e_sessao_e_atualizada(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models)
    _criar_usuario(main, usuarios, empresa, "Dona", "dona@exemplo.com", "senha-super-segura", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "dona@exemplo.com", "senha-super-segura")

        resposta = client.post(f"/admin/empresas/{empresa.id}/excluir", follow_redirects=False)
        assert resposta.status_code == 303
        assert resposta.headers["location"].startswith("/admin/dashboard?message=")

        # sessão não deve mais achar que a empresa (apagada) existe
        dashboard = client.get("/admin/dashboard")
        assert dashboard.status_code == 200
        assert "Bem-vindo" in dashboard.text


def test_admin_de_uma_empresa_nao_exclui_empresa_de_outra(monkeypatch, tmp_path):
    main, admin_module, models, usuarios = carregar_app(monkeypatch, tmp_path)

    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B")
    _criar_usuario(main, usuarios, empresa_a, "Dona A", "dona-a@exemplo.com", "senha-super-segura", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "dona-a@exemplo.com", "senha-super-segura")
        resposta = client.post(f"/admin/empresas/{empresa_b.id}/excluir", follow_redirects=False)

    assert resposta.status_code == 404

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 2
    finally:
        db.close()


def test_pagina_de_confirmacao_mostra_contagens(monkeypatch, tmp_path):
    main, admin_module, models, _ = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models)
    servico = _seed_servico(main, models, empresa)
    cliente = _seed_cliente(main, models, empresa)
    _seed_agendamento(main, models, empresa, cliente, servico)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)
        resposta = client.get(f"/admin/empresas/{empresa.id}/excluir")

    assert resposta.status_code == 200
    assert "1 serviço" in resposta.text
    assert "1 cliente" in resposta.text
    assert "1 agendamento" in resposta.text


def test_excluir_empresa_limpa_estado_transitorio_no_redis(monkeypatch, tmp_path):
    main, admin_module, models, _ = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models)
    redis_client_module = importlib.import_module("core.redis_client")
    redis_cliente = redis_client_module.redis_cliente
    redis_cliente.set(f"conversa:{empresa.id}:5511900000001", '{"passo": "aguardando_servico", "contexto": {}}')
    redis_cliente.set(f"ai:cache:{empresa.id}:algumhash", '{"intent": "consultar_servicos"}')
    redis_cliente.set(f"conversa:999999:5511900000002", '{"passo": "novo", "contexto": {}}')  # de outra empresa, não pode ser afetado

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)
        resposta = client.post(f"/admin/empresas/{empresa.id}/excluir", follow_redirects=False)

    assert resposta.status_code == 303
    assert redis_cliente.get(f"conversa:{empresa.id}:5511900000001") is None
    assert redis_cliente.get(f"ai:cache:{empresa.id}:algumhash") is None
    assert redis_cliente.get("conversa:999999:5511900000002") is not None  # não mexeu em chave de outra empresa


def test_mensagem_para_instancia_de_empresa_excluida_nao_processa_nada(monkeypatch, tmp_path):
    main, admin_module, models, _ = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, slug="clinica-a")

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)
        client.post(f"/admin/empresas/{empresa.id}/excluir", follow_redirects=False)

    conftest_module = importlib.import_module("conftest")

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            f"/webhook?token={conftest_module.WEBHOOK_SECRET}",
            json={
                "instance": "clinica-a",
                "data": {
                    "key": {"fromMe": False, "remoteJid": "5511900000001@s.whatsapp.net"},
                    "message": {"conversation": "oibot"},
                },
            },
        )

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "empresa_nao_encontrada"}
