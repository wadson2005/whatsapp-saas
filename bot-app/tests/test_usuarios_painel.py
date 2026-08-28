import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    usuarios = importlib.import_module("services.usuarios")
    security = importlib.import_module("core.security")
    main.ensure_schema()
    return main, models, usuarios, security


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


def _seed_servico(main, models, empresa, nome: str):
    db = main.SessionLocal()
    try:
        servico = models.Servico(empresa_id=empresa.id, nome=nome, duracao_minutos=30, ativo=True)
        db.add(servico)
        db.commit()
        db.refresh(servico)
        return servico
    finally:
        db.close()


def _seed_cliente(main, models, empresa, telefone: str, nome: str):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome)
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
        return cliente
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


def test_hash_de_senha_ida_e_volta(monkeypatch, tmp_path):
    _, _, _, security = carregar_app(monkeypatch, tmp_path)

    hash_gerado = security.hash_senha("minhasenha123")
    assert security.verificar_senha("minhasenha123", hash_gerado)
    assert not security.verificar_senha("senhaerrada", hash_gerado)


def test_login_usuario_de_empresa_com_credenciais_corretas(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _criar_usuario(main, usuarios, empresa, "Ana", "ana@clinica-a.com", "senha12345", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = _login(client, "ana@clinica-a.com", "senha12345")
        assert resposta.status_code == 303
        assert resposta.headers["location"] == "/admin/dashboard"


def test_login_usuario_inativo_ou_senha_errada_falha(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    usuario = _criar_usuario(main, usuarios, empresa, "Ana", "ana@clinica-a.com", "senha12345", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta_senha_errada = _login(client, "ana@clinica-a.com", "senha-errada")
        assert resposta_senha_errada.status_code == 401

        db = main.SessionLocal()
        try:
            usuario_db = db.query(models.UsuarioPainel).filter_by(id=usuario.id).first()
            usuario_db.ativo = False
            db.commit()
        finally:
            db.close()

        resposta_inativo = _login(client, "ana@clinica-a.com", "senha12345")
        assert resposta_inativo.status_code == 401


def test_usuario_operador_ve_apenas_a_propria_empresa(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B")
    _seed_cliente(main, models, empresa_a, "5511900000001", "Cliente A")
    cliente_b = _seed_cliente(main, models, empresa_b, "5511900000002", "Cliente B")
    _criar_usuario(main, usuarios, empresa_a, "Bruno", "bruno@clinica-a.com", "senha12345", usuarios.PAPEL_OPERADOR)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "bruno@clinica-a.com", "senha12345")

        resposta = client.get("/admin/clientes")
        assert resposta.status_code == 200
        assert "Cliente A" in resposta.text
        assert "Cliente B" not in resposta.text

        # tentativa de acessar cliente de outra empresa diretamente pelo id -> 404
        resposta_outra_empresa = client.get(f"/admin/clientes/{cliente_b.id}")
        assert resposta_outra_empresa.status_code == 404

        # tentativa de forçar o filtro por query string para a outra empresa -> ainda escopado
        resposta_forcada = client.get(f"/admin/clientes?empresa_id={empresa_b.id}")
        assert "Cliente B" not in resposta_forcada.text


def test_usuario_operador_nao_acessa_rotas_restritas_ao_papel_admin(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _criar_usuario(main, usuarios, empresa, "Bruno", "bruno@clinica-a.com", "senha12345", usuarios.PAPEL_OPERADOR)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "bruno@clinica-a.com", "senha12345")

        assert client.get("/admin/servicos/novo").status_code == 403
        assert client.post("/admin/servicos/novo", data={"empresa_id": str(empresa.id), "nome": "x", "duracao_minutos": "30"}).status_code == 403
        assert client.get("/admin/usuarios").status_code == 403
        assert client.get("/admin/empresas").status_code == 403
        assert client.get("/admin/configuracoes").status_code == 403


def test_usuario_admin_de_empresa_gerencia_usuarios_da_propria_empresa_mas_nao_configuracoes_globais(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _criar_usuario(main, usuarios, empresa, "Carla", "carla@clinica-a.com", "senha12345", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "carla@clinica-a.com", "senha12345")

        # admin da empresa pode criar operador na própria empresa
        resposta = client.post(
            "/admin/usuarios/novo",
            data={"empresa_id": str(empresa.id), "nome": "Novo Operador", "email": "op@clinica-a.com", "senha": "senha12345", "papel": "operador"},
            follow_redirects=False,
        )
        assert resposta.status_code == 303

        db = main.SessionLocal()
        try:
            criado = db.query(models.UsuarioPainel).filter_by(email="op@clinica-a.com").first()
            assert criado is not None
            assert criado.empresa_id == empresa.id
        finally:
            db.close()

        # mas configurações globais do sistema continuam superadmin-only
        assert client.get("/admin/configuracoes").status_code == 403
        assert client.get("/admin/empresas").status_code == 403


def test_usuario_nao_pode_desativar_a_propria_conta(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    usuario = _criar_usuario(main, usuarios, empresa, "Carla", "carla@clinica-a.com", "senha12345", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "carla@clinica-a.com", "senha12345")

        resposta = client.post(f"/admin/usuarios/{usuario.id}/toggle")
        assert resposta.status_code == 400


def test_criar_usuario_com_email_duplicado_falha(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _criar_usuario(main, usuarios, empresa, "Ana", "duplicado@clinica-a.com", "senha12345", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)

        resposta = client.post(
            "/admin/usuarios/novo",
            data={"empresa_id": str(empresa.id), "nome": "Outra Ana", "email": "duplicado@clinica-a.com", "senha": "senha12345", "papel": "operador"},
            follow_redirects=False,
        )
        assert resposta.status_code == 400
        assert "Já existe um usuário com esse e-mail" in resposta.text


def test_operador_nao_ve_botoes_de_edicao_em_servicos_e_conhecimento(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _seed_servico(main, models, empresa, "Corte")
    _criar_usuario(main, usuarios, empresa, "Bruno", "bruno@clinica-a.com", "senha12345", usuarios.PAPEL_OPERADOR)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "bruno@clinica-a.com", "senha12345")

        resposta = client.get("/admin/servicos")
        assert resposta.status_code == 200
        assert "Novo serviço" not in resposta.text
        assert "/admin/servicos/1/editar" not in resposta.text

        resposta_conhecimento = client.get("/admin/conhecimento")
        assert "Nova pergunta" not in resposta_conhecimento.text


def test_usuario_operador_navega_em_modo_leitura_pelas_telas_do_dia_a_dia(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _criar_usuario(main, usuarios, empresa, "Bruno", "bruno@clinica-a.com", "senha12345", usuarios.PAPEL_OPERADOR)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "bruno@clinica-a.com", "senha12345")

        for rota in (
            "/admin/dashboard",
            "/admin/servicos",
            "/admin/conhecimento",
            "/admin/agendamentos",
            "/admin/solicitacoes-atendimento",
            "/admin/insights",
            "/admin/clientes-inativos",
        ):
            resposta = client.get(rota)
            assert resposta.status_code == 200, f"{rota} retornou {resposta.status_code}"


def test_formularios_de_usuario_renderizam_para_admin_da_empresa(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    usuario = _criar_usuario(main, usuarios, empresa, "Carla", "carla@clinica-a.com", "senha12345", usuarios.PAPEL_ADMIN)

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "carla@clinica-a.com", "senha12345")

        assert client.get("/admin/usuarios/novo").status_code == 200
        assert client.get(f"/admin/usuarios/{usuario.id}/editar").status_code == 200


def test_superadmin_continua_com_acesso_total(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B")
    _seed_cliente(main, models, empresa_a, "5511900000001", "Cliente A")
    _seed_cliente(main, models, empresa_b, "5511900000002", "Cliente B")

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_superadmin(client)

        resposta = client.get("/admin/clientes")
        assert resposta.status_code == 200
        assert "Cliente A" in resposta.text
        assert "Cliente B" in resposta.text

        assert client.get("/admin/empresas").status_code == 200
        assert client.get("/admin/configuracoes").status_code == 200


def test_operador_nao_atualiza_agendamento_de_outra_empresa_via_id(monkeypatch, tmp_path):
    main, models, usuarios, _ = carregar_app(monkeypatch, tmp_path)
    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B")
    servico_b = _seed_servico(main, models, empresa_b, "Corte")
    cliente_b = _seed_cliente(main, models, empresa_b, "5511900000002", "Cliente B")
    _criar_usuario(main, usuarios, empresa_a, "Bruno", "bruno@clinica-a.com", "senha12345", usuarios.PAPEL_OPERADOR)

    db = main.SessionLocal()
    try:
        agendamento_b = models.Agendamento(
            empresa_id=empresa_b.id,
            cliente_final_id=cliente_b.id,
            servico_id=servico_b.id,
            data_hora=__import__("datetime").datetime(2026, 9, 1, 10, 0),
            duracao_minutos=30,
            status="agendado",
        )
        db.add(agendamento_b)
        db.commit()
        db.refresh(agendamento_b)
    finally:
        db.close()

    with TestClient(main.app, base_url="https://testserver") as client:
        _login(client, "bruno@clinica-a.com", "senha12345")

        resposta = client.post(f"/admin/agendamentos/{agendamento_b.id}/status", data={"status": "cancelado"})
        assert resposta.status_code == 404

        db = main.SessionLocal()
        try:
            agendamento_db = db.query(models.Agendamento).filter_by(id=agendamento_b.id).first()
            assert agendamento_db.status == "agendado"
        finally:
            db.close()
