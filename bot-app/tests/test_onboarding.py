import importlib
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import WEBHOOK_SECRET, preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    main.ensure_schema()
    return main


def _dados_passo2(**overrides):
    dados = {
        "telefone_whatsapp": "5586999999999",
        "horario_abertura": "08:00",
        "horario_fechamento": "18:00",
        "intervalo_entre_atendimentos_minutos": "15",
        "primeiro_servico_nome": "Consulta inicial",
        "primeiro_servico_duracao_minutos": "30",
        "primeiro_servico_preco": "120,00",
        "admin_nome": "Maria Souza",
        "admin_email": "maria@clinicasorrisofeliz.com",
        "admin_senha": "senha-super-segura",
    }
    dados.update(overrides)
    return dados


def _mockar_evolution(main, *, criar_ok=True, qrcode=None, estado="close", erro=None, ja_existe=False):
    from integrations.evolution_client import EvolutionAPIConexaoError

    if criar_ok:
        main.criar_instancia = AsyncMock(return_value={"instance": {"status": "created"}})
    else:
        main.criar_instancia = AsyncMock(side_effect=erro or EvolutionAPIConexaoError("Não foi possível conectar à Evolution API."))

    main.gerar_qrcode = AsyncMock(return_value=qrcode or {"pairingCode": "WZYEH1YY", "code": "2@abc123", "count": 1})
    main.estado_conexao = AsyncMock(return_value=estado)
    main.excluir_instancia = AsyncMock(return_value=None)
    main.instancia_existe = AsyncMock(return_value=ja_existe)


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
            data=_dados_passo2(),
            follow_redirects=False,
        )
        assert passo2.status_code == 303
        assert passo2.headers["location"] == "/onboarding/conectar"

        main.criar_instancia.assert_awaited_once()
        assert main.criar_instancia.call_args.args[0] == "clinica-sorriso-feliz"
        assert main.criar_instancia.call_args.args[2] == f"https://teste.exemplo.com/webhook?token={WEBHOOK_SECRET}"

        conectar = client.get("/onboarding/conectar")
        assert conectar.status_code == 200
        assert "WZYEH1YY" in conectar.text

        sucesso = client.get("/onboarding/sucesso")
        assert sucesso.status_code == 200
        assert "Clínica Sorriso Feliz" in sucesso.text

        # o cadastro já autentica o usuário admin recém-criado, sem passar por /admin/login
        dashboard = client.get("/admin/dashboard", follow_redirects=False)
        assert dashboard.status_code == 200

    db = main.SessionLocal()
    try:
        empresas = db.query(models.Empresa).all()
        servicos = db.query(models.Servico).all()
        usuarios = db.query(models.UsuarioPainel).all()
    finally:
        db.close()

    assert len(empresas) == 1
    assert len(servicos) == 1
    assert empresas[0].nome == "Clínica Sorriso Feliz"
    assert empresas[0].evolution_instance_name == "clinica-sorriso-feliz"
    assert servicos[0].nome == "Consulta inicial"

    assert len(usuarios) == 1
    assert usuarios[0].email == "maria@clinicasorrisofeliz.com"
    assert usuarios[0].papel == "admin"
    assert usuarios[0].empresa_id == empresas[0].id


def test_onboarding_nao_persiste_empresa_se_evolution_api_indisponivel(monkeypatch, tmp_path):
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
            data=_dados_passo2(),
            follow_redirects=False,
        )

    assert passo2.status_code == 502
    assert "Não foi possível conectar ao serviço de WhatsApp" in passo2.text

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
    finally:
        db.close()


def test_onboarding_mostra_motivo_real_quando_evolution_api_recusa(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    from integrations.evolution_client import EvolutionAPIError

    models = importlib.import_module("core.models")
    _mockar_evolution(main, criar_ok=False, erro=EvolutionAPIError("Esse número já está em uso por outra instância"))

    with TestClient(main.app) as client:
        client.post(
            "/onboarding",
            data={"nome": "Clínica Sorriso Feliz", "slug": "clinica-sorriso-feliz", "segmento": "clinica"},
            follow_redirects=False,
        )

        passo2 = client.post(
            "/onboarding/configurar",
            data=_dados_passo2(),
            follow_redirects=False,
        )

    assert passo2.status_code == 502
    assert "Esse número já está em uso por outra instância" in passo2.text

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
    finally:
        db.close()


def test_onboarding_rejeita_slug_ja_usado_na_evolution(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    models = importlib.import_module("core.models")
    _mockar_evolution(main, ja_existe=True)

    with TestClient(main.app) as client:
        client.post(
            "/onboarding",
            data={"nome": "Clínica Sorriso Feliz", "slug": "clinica-sorriso-feliz", "segmento": "clinica"},
            follow_redirects=False,
        )

        passo2 = client.post("/onboarding/configurar", data=_dados_passo2(), follow_redirects=False)

    assert passo2.status_code == 400
    assert "já existe uma instância de whatsapp" in passo2.text.lower()
    main.criar_instancia.assert_not_awaited()

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).count() == 0
    finally:
        db.close()


def test_onboarding_sugere_slug_valido_quando_nome_tem_acento(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        resposta = client.post(
            "/onboarding",
            data={"nome": "Salão Aurora & Cia", "slug": "Salão Aurora", "segmento": "salao"},
            follow_redirects=False,
        )

    assert resposta.status_code == 400
    assert "sugestão: salao-aurora" in resposta.text.lower()


def test_onboarding_rejeita_email_ja_cadastrado(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    models = importlib.import_module("core.models")
    from core.security import hash_senha

    _mockar_evolution(main)

    db = main.SessionLocal()
    try:
        outra_empresa = models.Empresa(nome="Barbearia do Zé", slug="barbearia-do-ze", segmento="barbearia", ativo=True)
        db.add(outra_empresa)
        db.flush()
        db.add(
            models.UsuarioPainel(
                empresa_id=outra_empresa.id,
                nome="Zé",
                email="maria@clinicasorrisofeliz.com",
                senha_hash=hash_senha("outra-senha-123"),
                papel="admin",
                ativo=True,
            )
        )
        db.commit()
    finally:
        db.close()

    with TestClient(main.app) as client:
        client.post(
            "/onboarding",
            data={"nome": "Clínica Sorriso Feliz", "slug": "clinica-sorriso-feliz", "segmento": "clinica"},
            follow_redirects=False,
        )

        passo2 = client.post("/onboarding/configurar", data=_dados_passo2(), follow_redirects=False)

    assert passo2.status_code == 400
    assert "Já existe um usuário com esse e-mail" in passo2.text
    main.criar_instancia.assert_not_awaited()

    db = main.SessionLocal()
    try:
        assert db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").count() == 0
    finally:
        db.close()


def test_onboarding_rejeita_senha_curta(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(main)

    with TestClient(main.app) as client:
        client.post(
            "/onboarding",
            data={"nome": "Clínica Sorriso Feliz", "slug": "clinica-sorriso-feliz", "segmento": "clinica"},
            follow_redirects=False,
        )

        passo2 = client.post("/onboarding/configurar", data=_dados_passo2(admin_senha="123"), follow_redirects=False)

    assert passo2.status_code == 400
    assert "pelo menos 8 caracteres" in passo2.text
    main.criar_instancia.assert_not_awaited()


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
            data=_dados_passo2(),
            follow_redirects=False,
        )

        status = client.get("/onboarding/conectar/status")
        assert status.status_code == 200
        assert status.json() == {"state": "open"}

        novo_qrcode = client.post("/onboarding/conectar/novo-qrcode")
        assert novo_qrcode.status_code == 200
        assert novo_qrcode.json()["pairingCode"] == "WZYEH1YY"
