import importlib
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    main.ensure_schema()
    return main


def _mockar_evolution(main, *, criar_ok=True, qrcode=None, estado="close"):
    from integrations.evolution_client import EvolutionAPIError

    if criar_ok:
        main.criar_instancia = AsyncMock(return_value={"instance": {"status": "created"}})
    else:
        main.criar_instancia = AsyncMock(side_effect=EvolutionAPIError("Evolution API indisponível"))

    main.gerar_qrcode = AsyncMock(return_value=qrcode or {"pairingCode": "WZYEH1YY", "code": "2@abc123", "count": 1})
    main.estado_conexao = AsyncMock(return_value=estado)
    main.excluir_instancia = AsyncMock(return_value=None)


def test_raiz_redireciona_para_onboarding_quando_nao_existe_empresa(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"


def test_onboarding_rejeita_slug_invalido(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.post(
            "/onboarding",
            data={
                "nome": "Clínica Sorriso Feliz",
                "slug": "Slug Invalido",
                "segmento": "clinica",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "slug" in response.text.lower()


def test_onboarding_cria_empresa_instancia_e_avanca_para_conectar(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    models = importlib.import_module("core.models")
    _mockar_evolution(main)

    with TestClient(main.app) as client:
        passo1 = client.post(
            "/onboarding",
            data={
                "nome": "Clínica Sorriso Feliz",
                "slug": "clinica-sorriso-feliz",
                "segmento": "clinica",
            },
            follow_redirects=False,
        )
        assert passo1.status_code == 303
        assert passo1.headers["location"] == "/onboarding/configurar"

        passo2 = client.post(
            "/onboarding/configurar",
            data={
                "telefone_whatsapp": "5586999999999",
                "horario_abertura": "08:00",
                "horario_fechamento": "18:00",
                "intervalo_entre_atendimentos_minutos": "15",
                "primeiro_servico_nome": "Consulta inicial",
                "primeiro_servico_duracao_minutos": "30",
                "primeiro_servico_preco": "120,00",
            },
            follow_redirects=False,
        )
        assert passo2.status_code == 303
        assert passo2.headers["location"] == "/onboarding/conectar"

        main.criar_instancia.assert_awaited_once()
        assert main.criar_instancia.call_args.args[0] == "clinica-sorriso-feliz"
        assert main.criar_instancia.call_args.args[2] == "https://teste.exemplo.com/webhook"

        conectar = client.get("/onboarding/conectar")
        assert conectar.status_code == 200
        assert "WZYEH1YY" in conectar.text

        sucesso = client.get("/onboarding/sucesso")

    assert sucesso.status_code == 200
    assert "Clínica Sorriso Feliz" in sucesso.text

    db = main.SessionLocal()
    try:
        empresas = db.query(models.Empresa).all()
        servicos = db.query(models.Servico).all()
    finally:
        db.close()

    assert len(empresas) == 1
    assert len(servicos) == 1
    assert empresas[0].nome == "Clínica Sorriso Feliz"
    assert empresas[0].evolution_instance_name == "clinica-sorriso-feliz"
    assert servicos[0].nome == "Consulta inicial"


def test_onboarding_nao_persiste_empresa_se_evolution_api_falhar(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    models = importlib.import_module("core.models")
    _mockar_evolution(main, criar_ok=False)

    with TestClient(main.app) as client:
        client.post(
            "/onboarding",
            data={"nome": "Clínica Sorriso Feliz", "slug": "clinica-sorriso-feliz", "segmento": "clinica"},
            follow_redirects=False,
        )

        passo2 = client.post(
            "/onboarding/configurar",
            data={
                "telefone_whatsapp": "5586999999999",
                "horario_abertura": "08:00",
                "horario_fechamento": "18:00",
                "intervalo_entre_atendimentos_minutos": "15",
                "primeiro_servico_nome": "Consulta inicial",
                "primeiro_servico_duracao_minutos": "30",
                "primeiro_servico_preco": "120,00",
            },
            follow_redirects=False,
        )

    assert passo2.status_code == 502
    assert "Não foi possível conectar ao WhatsApp" in passo2.text

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
    finally:
        db.close()


def test_onboarding_conectar_status_e_novo_qrcode(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(main, estado="open")

    with TestClient(main.app) as client:
        client.post(
            "/onboarding",
            data={"nome": "Clínica Sorriso Feliz", "slug": "clinica-sorriso-feliz", "segmento": "clinica"},
            follow_redirects=False,
        )
        client.post(
            "/onboarding/configurar",
            data={
                "telefone_whatsapp": "5586999999999",
                "horario_abertura": "08:00",
                "horario_fechamento": "18:00",
                "intervalo_entre_atendimentos_minutos": "15",
                "primeiro_servico_nome": "Consulta inicial",
                "primeiro_servico_duracao_minutos": "30",
                "primeiro_servico_preco": "120,00",
            },
            follow_redirects=False,
        )

        status = client.get("/onboarding/conectar/status")
        assert status.status_code == 200
        assert status.json() == {"state": "open"}

        novo_qrcode = client.post("/onboarding/conectar/novo-qrcode")
        assert novo_qrcode.status_code == 200
        assert novo_qrcode.json()["pairingCode"] == "WZYEH1YY"
