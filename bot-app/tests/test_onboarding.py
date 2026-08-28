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
    main.ensure_schema()
    return main, admin_module, models


def _dados_conta(**overrides):
    dados = {
        "nome": "Maria Souza",
        "email": "maria@exemplo.com",
        "senha": "senha-super-segura",
    }
    dados.update(overrides)
    return dados


def test_onboarding_cria_conta_sem_empresa_e_autentica(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/onboarding", data=_dados_conta(), follow_redirects=False)
        assert resposta.status_code == 303
        assert resposta.headers["location"] == "/admin/dashboard"

        dashboard = client.get("/admin/dashboard")
        assert dashboard.status_code == 200
        assert "Cadastrar minha empresa" in dashboard.text

    db = main.SessionLocal()
    try:
        usuario = db.query(models.UsuarioPainel).filter_by(email="maria@exemplo.com").one()
    finally:
        db.close()
    assert usuario.empresa_id is None
    assert usuario.papel == "operador"


def test_onboarding_rejeita_email_duplicado(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.get("/admin/logout")
        resposta = client.post("/onboarding", data=_dados_conta(nome="Outra Pessoa"), follow_redirects=False)

    assert resposta.status_code == 400
    assert "Já existe uma conta com esse e-mail" in resposta.text


def test_onboarding_rejeita_senha_curta(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/onboarding", data=_dados_conta(senha="123"), follow_redirects=False)

    assert resposta.status_code == 400
    assert "pelo menos 8 caracteres" in resposta.text


def test_onboarding_rejeita_nome_e_email_vazios(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/onboarding", data=_dados_conta(nome="", email=""), follow_redirects=False)

    assert resposta.status_code == 400
    assert "Informe seu nome" in resposta.text
    assert "Informe seu e-mail" in resposta.text


def test_onboarding_ja_autenticado_pula_para_dashboard(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        resposta = client.get("/onboarding", follow_redirects=False)

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/admin/dashboard"


def test_raiz_mostra_landing_para_visitante_anonimo(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.get("/")

    assert resposta.status_code == 200
    assert "Criar minha conta" in resposta.text


def test_raiz_redireciona_logado_para_dashboard(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        resposta = client.get("/", follow_redirects=False)

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/admin/dashboard"


def _mockar_evolution(admin_module, *, criar_ok=True, ja_existe=False, erro=None):
    from integrations.evolution_client import EvolutionAPIConexaoError

    if criar_ok:
        admin_module.criar_instancia = AsyncMock(return_value={"instance": {"status": "created"}})
    else:
        admin_module.criar_instancia = AsyncMock(side_effect=erro or EvolutionAPIConexaoError("Não foi possível conectar à Evolution API."))
    admin_module.excluir_instancia = AsyncMock(return_value=None)
    admin_module.gerar_qrcode = AsyncMock(return_value={"pairingCode": "WZYEH1YY", "code": "2@abc", "count": 1})
    admin_module.instancia_existe = AsyncMock(return_value=ja_existe)
    admin_module.estado_conexao = AsyncMock(return_value="close")


def _dados_empresa(**overrides):
    dados = {
        "nome": "Clínica Sorriso Feliz",
        "slug": "clinica-sorriso-feliz",
        "segmento": "clinica",
        "telefone_whatsapp": "5586999999999",
    }
    dados.update(overrides)
    return dados


def test_cadastrar_empresa_self_service_vincula_usuario_como_admin(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())

        resposta = client.post("/admin/empresas/cadastrar", data=_dados_empresa(), follow_redirects=False)
        assert resposta.status_code == 303
        assert "/conectar" in resposta.headers["location"]

        admin_module.criar_instancia.assert_awaited_once()
        assert admin_module.criar_instancia.call_args.args[0] == "clinica-sorriso-feliz"

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
        usuario = db.query(models.UsuarioPainel).filter_by(email="maria@exemplo.com").one()
    finally:
        db.close()

    assert empresa.ativo is False
    assert empresa.ativado_em is None
    assert usuario.empresa_id == empresa.id
    assert usuario.papel == "admin"
    # configuração rápida: já nasce pronta pra funcionar, sem exigir passo extra
    assert empresa.atendimento_automatico_ativo is True
    assert empresa.permitir_atendimento_humano is True
    assert empresa.palavra_ativacao == "oibot"
    assert empresa.mensagem_boas_vindas and "Clínica Sorriso Feliz" in empresa.mensagem_boas_vindas


def test_cadastrar_empresa_bloqueado_para_quem_ja_tem_empresa(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        resposta = client.post(
            "/admin/empresas/cadastrar",
            data=_dados_empresa(slug="segunda-empresa"),
            follow_redirects=False,
        )

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/admin/dashboard"
    admin_module.criar_instancia.assert_awaited_once()  # não chamou de novo pra segunda tentativa


def test_rotas_de_empresa_redirecionam_usuario_sem_empresa_para_dashboard(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())

        resposta = client.get("/admin/servicos", follow_redirects=False)

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/admin/dashboard"


def test_ativar_bot_exige_estar_pronto_mas_rota_sempre_funciona_e_marca_ativado_em(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        db = main.SessionLocal()
        try:
            empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
        finally:
            db.close()

        resposta = client.post(f"/admin/empresas/{empresa.id}/ativar", follow_redirects=False)
        assert resposta.status_code == 303

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
    finally:
        db.close()

    assert empresa.ativo is True
    assert empresa.ativado_em is not None


def test_pausar_bot_mantem_ativado_em_para_distinguir_de_nunca_configurado(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        db = main.SessionLocal()
        try:
            empresa_id = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one().id
        finally:
            db.close()

        client.post(f"/admin/empresas/{empresa_id}/ativar")
        client.post(f"/admin/empresas/{empresa_id}/pausar")

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).filter_by(id=empresa_id).one()
    finally:
        db.close()

    assert empresa.ativo is False
    assert empresa.ativado_em is not None


def test_configurar_bot_hub_reflete_estado_real(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        hub_sem_servico = client.get("/admin/configurar-bot")
        assert "Ative seu bot" in hub_sem_servico.text or "🔒" in hub_sem_servico.text

        db = main.SessionLocal()
        try:
            empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
            servico = models.Servico(empresa_id=empresa.id, nome="Corte", duracao_minutos=30, ativo=True)
            db.add(servico)
            db.commit()
        finally:
            db.close()

        admin_module.estado_conexao = AsyncMock(return_value="open")
        hub_pronto = client.get("/admin/configurar-bot")
        assert "Tudo pronto pra ativar" in hub_pronto.text


def test_configurar_bot_atendimento_salva_mensagens_customizadas(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        resposta = client.post(
            "/admin/configurar-bot/atendimento",
            data={"mensagem_boas_vindas": "Olá! Bem-vindo à Clínica Sorriso Feliz."},
            follow_redirects=False,
        )
        assert resposta.status_code == 303

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
    finally:
        db.close()
    assert empresa.mensagem_boas_vindas == "Olá! Bem-vindo à Clínica Sorriso Feliz."


def test_configurar_bot_atendimento_mostra_tudo_pre_preenchido(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        pagina = client.get("/admin/configurar-bot/atendimento")

    assert pagina.status_code == 200
    assert "oibot" in pagina.text
    assert "Clínica Sorriso Feliz" in pagina.text


def test_configurar_bot_atendimento_salva_palavra_de_ativacao_customizada(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        resposta = client.post(
            "/admin/configurar-bot/atendimento",
            data={"palavra_ativacao": "quero marcar, agendar"},
            follow_redirects=False,
        )
        assert resposta.status_code == 303

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
    finally:
        db.close()
    assert empresa.palavra_ativacao == "quero marcar, agendar"


def test_configurar_bot_lembretes_salva_canal_escolhido(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        pagina = client.get("/admin/configurar-bot/lembretes")
        assert pagina.status_code == 200
        assert "Lembrete por e-mail" in pagina.text

        resposta = client.post(
            "/admin/configurar-bot/lembretes",
            data={"lembrete_canal_email": "on"},
            follow_redirects=False,
        )
        assert resposta.status_code == 303

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
    finally:
        db.close()
    assert empresa.lembrete_canal_email is True


def _login(client: TestClient, email: str, senha: str = "senha-super-segura"):
    return client.post("/admin/login", data={"username": email, "password": senha})


def test_isolamento_multi_tenant_nas_rotas_de_ativar_pausar_e_hub(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta(email="dona-a@exemplo.com"))
        client.post(
            "/admin/empresas/cadastrar",
            data=_dados_empresa(slug="empresa-a", nome="Empresa A", telefone_whatsapp="5586999999901"),
        )
        client.get("/admin/logout")

        client.post("/onboarding", data=_dados_conta(email="dono-b@exemplo.com"))
        client.post(
            "/admin/empresas/cadastrar",
            data=_dados_empresa(slug="empresa-b", nome="Empresa B", telefone_whatsapp="5586999999902"),
        )
        client.get("/admin/logout")

    db = main.SessionLocal()
    try:
        empresa_a = db.query(models.Empresa).filter_by(slug="empresa-a").one()
        empresa_b = db.query(models.Empresa).filter_by(slug="empresa-b").one()
    finally:
        db.close()

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "dona-a@exemplo.com")

        assert client.post(f"/admin/empresas/{empresa_b.id}/ativar").status_code == 404
        assert client.post(f"/admin/empresas/{empresa_b.id}/pausar").status_code == 404

        hub = client.get(f"/admin/configurar-bot?empresa_id={empresa_b.id}")
        assert "Empresa A" in hub.text
        assert "Empresa B" not in hub.text

    db = main.SessionLocal()
    try:
        empresa_b_depois = db.query(models.Empresa).filter_by(id=empresa_b.id).one()
    finally:
        db.close()
    assert empresa_b_depois.ativo is False
    assert empresa_b_depois.ativado_em is None
    assert empresa_a.ativo is False


def test_whatsapp_conectado_nao_implica_bot_ativo(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        admin_module.estado_conexao = AsyncMock(return_value="open")
        hub = client.get("/admin/configurar-bot")
        assert "Ative seu bot" in hub.text or "Tudo pronto pra ativar" in hub.text

    db = main.SessionLocal()
    try:
        empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
    finally:
        db.close()
    assert empresa.ativo is False


def test_dashboard_mostra_status_de_configuracao_pendente(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        dashboard = client.get("/admin/dashboard")
        assert "Continuar configuração" in dashboard.text


def test_usuario_retorna_apos_logout_e_ve_progresso_persistido(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    _mockar_evolution(admin_module)

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post("/onboarding", data=_dados_conta())
        client.post("/admin/empresas/cadastrar", data=_dados_empresa())

        db = main.SessionLocal()
        try:
            empresa = db.query(models.Empresa).filter_by(slug="clinica-sorriso-feliz").one()
            servico = models.Servico(empresa_id=empresa.id, nome="Corte", duracao_minutos=30, ativo=True)
            db.add(servico)
            db.commit()
        finally:
            db.close()

        client.get("/admin/logout")

        # nao autenticado, tentar acessar hub -> vai pro login
        resposta_anonima = client.get("/admin/configurar-bot", follow_redirects=False)
        assert resposta_anonima.status_code == 303
        assert resposta_anonima.headers["location"] == "/admin/login"

        _login(client, "maria@exemplo.com")
        admin_module.estado_conexao = AsyncMock(return_value="open")
        hub = client.get("/admin/configurar-bot")

    assert "1 serviço(s) ativo(s)" in hub.text
    assert "Tudo pronto pra ativar" in hub.text


def test_rotas_antigas_de_onboarding_nao_existem_mais(monkeypatch, tmp_path):
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        assert client.get("/onboarding/configurar").status_code == 404
        assert client.get("/onboarding/conectar").status_code == 404
        assert client.get("/onboarding/conectar/status").status_code == 404
        assert client.post("/onboarding/conectar/novo-qrcode").status_code == 404
        assert client.get("/onboarding/sucesso").status_code == 404


def test_criar_usuario_com_email_duplicado_levanta_integrity_error(monkeypatch, tmp_path):
    """Valida a premissa do tratamento de corrida em `onboarding_submit`: um segundo
    `criar_usuario` com o mesmo e-mail (como aconteceria se dois cadastros simultâneos
    passassem pela pre-checagem ao mesmo tempo) precisa estourar `IntegrityError` — é
    esse tipo de exceção que a rota captura para não devolver 500 nesse cenário."""
    main, admin_module, models = carregar_app(monkeypatch, tmp_path)
    usuarios = importlib.import_module("services.usuarios")
    from sqlalchemy.exc import IntegrityError

    db = main.SessionLocal()
    try:
        usuarios.criar_usuario(db, nome="Maria", email="maria@exemplo.com", senha="senha-antiga-123", papel="operador")
        try:
            usuarios.criar_usuario(db, nome="Outra Maria", email="maria@exemplo.com", senha="outra-senha-123", papel="operador")
            assert False, "esperava IntegrityError"
        except IntegrityError:
            pass
    finally:
        db.close()
