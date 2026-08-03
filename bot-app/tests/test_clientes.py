import importlib
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    main.ensure_schema()
    return main, models


def _seed_empresa(main, models, slug: str, nome: str, telefone: str, instancia: str):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome=nome,
            slug=slug,
            segmento="clinica",
            telefone_whatsapp=telefone,
            evolution_instance_name=instancia,
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


def _seed_servico(main, models, empresa, nome: str, duracao: int = 30):
    db = main.SessionLocal()
    try:
        servico = models.Servico(empresa_id=empresa.id, nome=nome, duracao_minutos=duracao, ativo=True)
        db.add(servico)
        db.commit()
        db.refresh(servico)
        return servico
    finally:
        db.close()


def _seed_cliente(main, models, empresa, telefone: str, nome: str, criado_em: datetime):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome, criado_em=criado_em)
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
        return cliente
    finally:
        db.close()


def _seed_agendamento(main, models, empresa, cliente, servico, data_hora: datetime, status: str = "agendado"):
    db = main.SessionLocal()
    try:
        agendamento = models.Agendamento(
            empresa_id=empresa.id,
            cliente_final_id=cliente.id,
            servico_id=servico.id,
            data_hora=data_hora,
            duracao_minutos=servico.duracao_minutos,
            status=status,
        )
        db.add(agendamento)
        db.commit()
        db.refresh(agendamento)
        return agendamento
    finally:
        db.close()


def _login_admin(client: TestClient):
    resposta = client.post(
        "/admin/login",
        data={"username": "admin", "password": "senha-super-segura-123"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303


def test_lista_clientes_isola_por_empresa_e_busca(monkeypatch, tmp_path):
    main, models = carregar_app(monkeypatch, tmp_path)

    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B", "5511999999992", "instancia-b")

    _seed_cliente(main, models, empresa_a, "5511900000001", "Ana Souza", datetime(2026, 7, 1, 10, 0))
    _seed_cliente(main, models, empresa_a, "5511900000002", "Bruno Lima", datetime(2026, 7, 2, 10, 0))
    _seed_cliente(main, models, empresa_b, "5511900000003", "Carla Dias", datetime(2026, 7, 3, 10, 0))

    with TestClient(main.app) as client:
        _login_admin(client)

        resposta = client.get(f"/admin/clientes?empresa_id={empresa_a.id}")
        assert resposta.status_code == 200
        assert "Ana Souza" in resposta.text
        assert "Bruno Lima" in resposta.text
        assert "Carla Dias" not in resposta.text

        resposta_busca = client.get(f"/admin/clientes?empresa_id={empresa_a.id}&q=Ana")
        assert "Ana Souza" in resposta_busca.text
        assert "Bruno Lima" not in resposta_busca.text


def test_lista_clientes_ordena_por_nome(monkeypatch, tmp_path):
    main, models = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    _seed_cliente(main, models, empresa, "5511900000001", "Zeca", datetime(2026, 7, 1, 10, 0))
    _seed_cliente(main, models, empresa, "5511900000002", "Ana", datetime(2026, 7, 2, 10, 0))

    with TestClient(main.app) as client:
        _login_admin(client)

        resposta = client.get(f"/admin/clientes?empresa_id={empresa.id}&sort=nome")
        assert resposta.status_code == 200
        assert resposta.text.index("Ana") < resposta.text.index("Zeca")


def test_detalhe_cliente_mostra_historico_e_isola_por_empresa(monkeypatch, tmp_path):
    main, models = carregar_app(monkeypatch, tmp_path)

    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B", "5511999999992", "instancia-b")
    servico = _seed_servico(main, models, empresa_a, "Corte")

    cliente = _seed_cliente(main, models, empresa_a, "5511900000001", "Ana Souza", datetime(2026, 7, 1, 10, 0))
    _seed_agendamento(main, models, empresa_a, cliente, servico, datetime(2026, 7, 10, 14, 0), status="confirmado")

    with TestClient(main.app) as client:
        _login_admin(client)

        resposta = client.get(f"/admin/clientes/{cliente.id}")
        assert resposta.status_code == 200
        assert "Ana Souza" in resposta.text
        assert "Corte" in resposta.text
        assert "confirmado" in resposta.text

        resposta_outra_empresa = client.get(f"/admin/clientes/{cliente.id}?empresa_id={empresa_b.id}")
        assert resposta_outra_empresa.status_code == 404


def test_dashboard_exibe_metricas_de_clientes_e_solicitacoes(monkeypatch, tmp_path):
    main, models = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", datetime(2026, 7, 1, 10, 0))

    with TestClient(main.app) as client:
        _login_admin(client)

        resposta = client.get("/admin/dashboard")
        assert resposta.status_code == 200
        assert "Clientes" in resposta.text
        assert "Solicitações pendentes" in resposta.text


def test_lista_agendamentos_linka_para_detalhe_do_cliente(monkeypatch, tmp_path):
    main, models = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", datetime(2026, 7, 1, 10, 0))
    _seed_agendamento(main, models, empresa, cliente, servico, datetime(2026, 7, 10, 14, 0))

    with TestClient(main.app) as client:
        _login_admin(client)

        resposta = client.get("/admin/agendamentos")
        assert resposta.status_code == 200
        assert f"/admin/clientes/{cliente.id}" in resposta.text
