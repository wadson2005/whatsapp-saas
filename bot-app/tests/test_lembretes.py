import asyncio
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_ENV = {
    "REDIS_URL": "redis://localhost:6379/1",
    "EVOLUTION_API_KEY": "x",
    "META_TOKEN": "x",
    "META_PHONE_NUMBER_ID": "x",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "senha-super-segura-123",
    "SESSION_SECRET_KEY": "0123456789abcdef0123456789abcdef",
}


def carregar_app(monkeypatch, tmp_path: Path):
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    database_path = tmp_path / "bot-app.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    for chave, valor in BOOTSTRAP_ENV.items():
        monkeypatch.setenv(chave, valor)

    for modulo in [
        "main",
        "admin",
        "config",
        "database",
        "models",
        "schema",
        "conversa",
        "redis_client",
        "agenda",
        "meta_client",
        "atendimento_humano",
        "lembretes",
        "ai",
        "ai.provider",
        "ai.service",
        "ai.prompts",
        "ai.models",
        "ai.cache",
        "texto_utils",
        "conhecimento",
        "metricas",
    ]:
        sys.modules.pop(modulo, None)

    main = importlib.import_module("main")
    models = importlib.import_module("models")
    lembretes = importlib.import_module("lembretes")
    agenda = importlib.import_module("agenda")
    main.ensure_schema()
    return main, models, lembretes, agenda


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


def _seed_agendamento(
    main,
    models,
    empresa,
    cliente,
    servico,
    data_hora: datetime,
    status: str = "agendado",
    lembrete_enviado_em: datetime | None = None,
):
    db = main.SessionLocal()
    try:
        agendamento = models.Agendamento(
            empresa_id=empresa.id,
            cliente_final_id=cliente.id,
            servico_id=servico.id,
            data_hora=data_hora,
            duracao_minutos=servico.duracao_minutos,
            status=status,
            lembrete_enviado_em=lembrete_enviado_em,
        )
        db.add(agendamento)
        db.commit()
        db.refresh(agendamento)
        return agendamento
    finally:
        db.close()


def test_agendamento_na_janela_recebe_lembrete_e_marca_enviado(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_template = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    agendamento = _seed_agendamento(
        main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=2)
    )

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 1
    assert lembretes.enviar_template.await_count == 1

    db = main.SessionLocal()
    try:
        atualizado = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
        assert atualizado.lembrete_enviado_em is not None
    finally:
        db.close()


def test_agendamento_fora_da_janela_nao_recebe_lembrete(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_template = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=48))

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_template.assert_not_awaited()


def test_agendamento_ja_com_lembrete_nao_reenvia(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_template = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    _seed_agendamento(
        main,
        models,
        empresa,
        cliente,
        servico,
        datetime.utcnow() + timedelta(hours=2),
        lembrete_enviado_em=datetime.utcnow() - timedelta(hours=1),
    )

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_template.assert_not_awaited()


def test_agendamento_cancelado_nao_recebe_lembrete(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_template = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    _seed_agendamento(
        main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=2), status="cancelado"
    )

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_template.assert_not_awaited()


def test_agendamento_passado_nao_recebe_lembrete(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_template = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() - timedelta(hours=1))

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_template.assert_not_awaited()


def test_falha_no_envio_nao_marca_como_enviado_e_nao_interrompe_lote(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente_falha = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    cliente_ok = _seed_cliente(main, models, empresa, "5511900000002", "Bruno Lima")
    agendamento_falha = _seed_agendamento(
        main, models, empresa, cliente_falha, servico, datetime.utcnow() + timedelta(hours=1)
    )
    agendamento_ok = _seed_agendamento(
        main, models, empresa, cliente_ok, servico, datetime.utcnow() + timedelta(hours=2)
    )

    respostas = [{"error": {"message": "template não aprovado"}}, {"messages": [{"id": "wamid.ok"}]}]
    lembretes.enviar_template = AsyncMock(side_effect=respostas)

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 1
    assert lembretes.enviar_template.await_count == 2

    db = main.SessionLocal()
    try:
        falha = db.query(models.Agendamento).filter_by(id=agendamento_falha.id).first()
        ok = db.query(models.Agendamento).filter_by(id=agendamento_ok.id).first()
        assert falha.lembrete_enviado_em is None
        assert ok.lembrete_enviado_em is not None
    finally:
        db.close()


def test_parametros_do_template_incluem_nome_servico_data_e_empresa(monkeypatch, tmp_path):
    main, models, lembretes, agenda = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_template = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica Sorriso Feliz", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte de cabelo")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    data_hora = datetime.utcnow() + timedelta(hours=2)
    _seed_agendamento(main, models, empresa, cliente, servico, data_hora)

    db = main.SessionLocal()
    try:
        asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    _, kwargs = lembretes.enviar_template.await_args
    assert kwargs["numero"] == "5511900000001"
    assert kwargs["parametros_corpo"] == [
        "Ana Souza",
        "Corte de cabelo",
        agenda.formatar_data_hora(data_hora),
        "Clínica Sorriso Feliz",
    ]


def test_reagendamento_reseta_lembrete_enviado_em(monkeypatch, tmp_path):
    main, models, lembretes, agenda = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza")
    agendamento = _seed_agendamento(
        main,
        models,
        empresa,
        cliente,
        servico,
        datetime.utcnow() + timedelta(hours=2),
        lembrete_enviado_em=datetime.utcnow() - timedelta(hours=1),
    )

    db = main.SessionLocal()
    try:
        agendamento_db = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
        empresa_db = db.query(models.Empresa).filter_by(id=empresa.id).first()
        novo_horario = datetime(2026, 8, 10, 10, 0)  # segunda-feira, dentro do horário de funcionamento
        atualizado, validacao = agenda.reagendar_agendamento(db, empresa_db, agendamento_db, novo_horario)
        assert validacao.ok
        assert atualizado.lembrete_enviado_em is None
    finally:
        db.close()
