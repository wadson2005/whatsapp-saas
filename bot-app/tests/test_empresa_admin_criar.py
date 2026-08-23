import importlib
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import WEBHOOK_SECRET, preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    admin_module = importlib.import_module("admin")
    models = importlib.import_module("core.models")
    main.ensure_schema()
    return main, admin_module, models


def _mockar_evolution(admin_module, *, criar_ok=True, erro=None):
    from integrations.evolution_client import EvolutionAPIConexaoError

    if criar_ok:
        admin_module.criar_instancia = AsyncMock(return_value={"instance": {"status": "created"}})
    else:
        admin_module.criar_instancia = AsyncMock(side_effect=erro or EvolutionAPIConexaoError("Não foi possível conectar à Evolution API."))
    admin_module.excluir_instancia = AsyncMock(return_value=None)
    admin_module.gerar_qrcode = AsyncMock(return_value={"pairingCode": "WZYEH1YY", "code": "2@abc", "count": 1})


def _login_superadmin(client: TestClient):
    resposta = client.post(
        "/admin/login",
        data={"username": "admin", "password": "senha-super-segura-123"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303


def _dados_empresa(**overrides):
    dados = {
        "nome": "Clínica Sorriso Feliz",
        "slug": "clinica-sorriso-feliz",
        "segmento": "clinica",
        "telefone_whatsapp": "5586999999999",
        "horario_abertura": "08:00",
        "horario_fechamento": "18:00",
        "intervalo_entre_atendimentos_minutos": "15",
        "ativo": "on",
    }
    dados.update(overrides)
    return dados


def test_criar_empresa_cria_instancia_automaticamente_e_redireciona_para_conectar(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app) as client:
        _login_superadmin(client)

        resposta = client.post("/admin/empresas/nova", data=_dados_empresa(), follow_redirects=False)

    assert resposta.status_code == 303
    assert resposta.headers["location"].startswith("/admin/empresas/")
    assert "/conectar?message=" in resposta.headers["location"]

    admin_module.criar_instancia.assert_awaited_once()
    assert admin_module.criar_instancia.call_args.args[0] == "clinica-sorriso-feliz"
    assert admin_module.criar_instancia.call_args.args[1] == "5586999999999"
    assert admin_module.criar_instancia.call_args.args[2] == f"https://teste.exemplo.com/webhook?token={WEBHOOK_SECRET}"

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).one()
    finally:
        db.close()

    assert empresa.evolution_instance_name == "clinica-sorriso-feliz"
    assert empresa.telefone_whatsapp == "5586999999999"


def test_criar_empresa_sem_telefone_nao_chama_evolution(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app) as client:
        _login_superadmin(client)

        resposta = client.post(
            "/admin/empresas/nova",
            data=_dados_empresa(telefone_whatsapp=""),
            follow_redirects=False,
        )

    assert resposta.status_code == 400
    assert "telefone" in resposta.text.lower()
    admin_module.criar_instancia.assert_not_awaited()

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
    finally:
        db.close()


def test_criar_empresa_nao_persiste_se_evolution_api_indisponivel(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module, criar_ok=False)

    with TestClient(main.app) as client:
        _login_superadmin(client)

        resposta = client.post("/admin/empresas/nova", data=_dados_empresa(), follow_redirects=False)

    assert resposta.status_code == 502
    assert "Não foi possível conectar ao serviço de WhatsApp" in resposta.text

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
    finally:
        db.close()


def test_criar_empresa_mostra_motivo_real_quando_evolution_api_recusa(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    from integrations.evolution_client import EvolutionAPIError

    _mockar_evolution(admin_module, criar_ok=False, erro=EvolutionAPIError("Esse número já está em uso por outra instância"))

    with TestClient(main.app) as client:
        _login_superadmin(client)

        resposta = client.post("/admin/empresas/nova", data=_dados_empresa(), follow_redirects=False)

    assert resposta.status_code == 502
    assert "Esse número já está em uso por outra instância" in resposta.text

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
    finally:
        db.close()


def test_formulario_de_empresa_nao_pede_mais_instancia_evolution(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        _login_superadmin(client)

        resposta = client.get("/admin/empresas/nova")

    assert resposta.status_code == 200
    assert "evolution_instance_name" not in resposta.text
