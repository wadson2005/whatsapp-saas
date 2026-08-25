import importlib
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    metricas = importlib.import_module("services.metricas")
    main.ensure_schema()
    return main, models, metricas


def _seed_empresa(main, models, slug: str, nome: str):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome=nome,
            slug=slug,
            segmento="clinica",
            telefone_whatsapp="5511999999990",
            evolution_instance_name=slug,
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


def _seed_cliente(main, models, empresa, telefone: str, nome: str, criado_em=None):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome, criado_em=criado_em or datetime.utcnow())
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
        return cliente
    finally:
        db.close()


def _seed_agendamento(main, models, empresa, cliente, servico, data_hora):
    db = main.SessionLocal()
    try:
        agendamento = models.Agendamento(
            empresa_id=empresa.id,
            cliente_final_id=cliente.id,
            servico_id=servico.id,
            data_hora=data_hora,
            duracao_minutos=servico.duracao_minutos,
            status="agendado",
        )
        db.add(agendamento)
        db.commit()
    finally:
        db.close()


def _login_admin(client: TestClient):
    resposta = client.post(
        "/admin/login",
        data={"username": "admin", "password": "senha-super-segura-123"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303


def test_cliente_sem_atendimento_recente_aparece(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana", criado_em=datetime.utcnow() - timedelta(days=200))
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() - timedelta(days=120))

    db = main.SessionLocal()
    try:
        inativos = metricas.listar_clientes_inativos(db, empresa.id, 90)
    finally:
        db.close()

    assert len(inativos) == 1
    assert inativos[0]["nome"] == "Ana"


def test_cliente_com_atendimento_recente_nao_aparece(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana")
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() - timedelta(days=5))

    db = main.SessionLocal()
    try:
        inativos = metricas.listar_clientes_inativos(db, empresa.id, 90)
    finally:
        db.close()

    assert inativos == []


def test_cliente_sem_agendamento_usa_data_de_cadastro(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _seed_cliente(main, models, empresa, "5511900000001", "Ana", criado_em=datetime.utcnow() - timedelta(days=200))

    db = main.SessionLocal()
    try:
        inativos = metricas.listar_clientes_inativos(db, empresa.id, 90)
    finally:
        db.close()

    assert len(inativos) == 1
    assert inativos[0]["ultimo_atendimento"] is None


def test_filtro_por_dias_muda_resultado(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana")
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() - timedelta(days=45))

    db = main.SessionLocal()
    try:
        inativos_30 = metricas.listar_clientes_inativos(db, empresa.id, 30)
        inativos_60 = metricas.listar_clientes_inativos(db, empresa.id, 60)
    finally:
        db.close()

    assert len(inativos_30) == 1
    assert len(inativos_60) == 0


def test_isolamento_por_empresa(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B")
    _seed_cliente(main, models, empresa_a, "5511900000001", "Ana", criado_em=datetime.utcnow() - timedelta(days=200))
    _seed_cliente(main, models, empresa_b, "5511900000002", "Bruno", criado_em=datetime.utcnow() - timedelta(days=200))

    db = main.SessionLocal()
    try:
        inativos_a = metricas.listar_clientes_inativos(db, empresa_a.id, 90)
    finally:
        db.close()

    assert len(inativos_a) == 1
    assert inativos_a[0]["nome"] == "Ana"


def test_pagina_admin_renderiza(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    _seed_cliente(main, models, empresa, "5511900000001", "Ana", criado_em=datetime.utcnow() - timedelta(days=200))

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_admin(client)
        resposta = client.get(f"/admin/clientes-inativos?empresa_id={empresa.id}&dias=90")

    assert resposta.status_code == 200
    assert "Ana" in resposta.text
