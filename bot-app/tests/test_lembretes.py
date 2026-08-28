import asyncio
import importlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    lembretes = importlib.import_module("services.lembretes")
    agenda = importlib.import_module("services.agenda")
    main.ensure_schema()
    return main, models, lembretes, agenda


def _proxima_segunda_10h() -> datetime:
    hoje = datetime.now()
    dias_ate_segunda = (7 - hoje.weekday()) % 7 or 7
    proxima_segunda = hoje + timedelta(days=dias_ate_segunda)
    return proxima_segunda.replace(hour=10, minute=0, second=0, microsecond=0)


def _seed_empresa(
    main,
    models,
    slug: str,
    nome: str,
    telefone: str,
    instancia: str,
    lembrete_canal_email: bool = True,
):
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
            lembrete_canal_email=lembrete_canal_email,
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


def _seed_cliente(main, models, empresa, telefone: str, nome: str, email: str | None = None):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome, email=email)
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
    lembrete_email_enviado_em: datetime | None = None,
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
            lembrete_email_enviado_em=lembrete_email_enviado_em,
        )
        db.add(agendamento)
        db.commit()
        db.refresh(agendamento)
        return agendamento
    finally:
        db.close()


def test_agendamento_na_janela_recebe_lembrete_e_marca_enviado(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    agendamento = _seed_agendamento(
        main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=2)
    )

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 1
    lembretes.enviar_email.assert_awaited_once()

    db = main.SessionLocal()
    try:
        atualizado = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
        assert atualizado.lembrete_email_enviado_em is not None
    finally:
        db.close()


def test_agendamento_fora_da_janela_nao_recebe_lembrete(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=48))

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_email.assert_not_awaited()


def test_agendamento_ja_com_lembrete_nao_reenvia(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    _seed_agendamento(
        main,
        models,
        empresa,
        cliente,
        servico,
        datetime.utcnow() + timedelta(hours=2),
        lembrete_email_enviado_em=datetime.utcnow() - timedelta(hours=1),
    )

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_email.assert_not_awaited()


def test_agendamento_cancelado_nao_recebe_lembrete(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    _seed_agendamento(
        main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=2), status="cancelado"
    )

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_email.assert_not_awaited()


def test_agendamento_passado_nao_recebe_lembrete(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() - timedelta(hours=1))

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_email.assert_not_awaited()


def test_canal_desativado_nao_recebe_lembrete(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(
        main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a", lembrete_canal_email=False
    )
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=2))

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_email.assert_not_awaited()


def test_cliente_sem_email_fecha_ciclo_sem_repetir_a_cada_rodada(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email=None)
    agendamento = _seed_agendamento(main, models, empresa, cliente, servico, datetime.utcnow() + timedelta(hours=2))

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 0
    lembretes.enviar_email.assert_not_awaited()

    db = main.SessionLocal()
    try:
        atualizado = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
        assert atualizado.lembrete_email_enviado_em is not None  # canal fechado, mesmo sem tentativa real
    finally:
        db.close()

    # próximo ciclo: não reaparece na busca nem tenta enviar de novo
    db = main.SessionLocal()
    try:
        enviados_2 = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados_2 == 0
    lembretes.enviar_email.assert_not_awaited()


def test_falha_no_envio_nao_marca_como_enviado_e_nao_interrompe_lote(monkeypatch, tmp_path):
    main, models, lembretes, _ = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente_falha = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    cliente_ok = _seed_cliente(main, models, empresa, "5511900000002", "Bruno Lima", email="bruno@exemplo.com")
    agendamento_falha = _seed_agendamento(
        main, models, empresa, cliente_falha, servico, datetime.utcnow() + timedelta(hours=1)
    )
    agendamento_ok = _seed_agendamento(
        main, models, empresa, cliente_ok, servico, datetime.utcnow() + timedelta(hours=2)
    )

    lembretes.enviar_email = AsyncMock(side_effect=[lembretes.EmailError("Resend recusou o envio"), None])

    db = main.SessionLocal()
    try:
        enviados = asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    assert enviados == 1
    assert lembretes.enviar_email.await_count == 2

    db = main.SessionLocal()
    try:
        falha = db.query(models.Agendamento).filter_by(id=agendamento_falha.id).first()
        ok = db.query(models.Agendamento).filter_by(id=agendamento_ok.id).first()
        assert falha.lembrete_email_enviado_em is None
        assert ok.lembrete_email_enviado_em is not None
    finally:
        db.close()


def test_email_inclui_nome_servico_data_e_empresa(monkeypatch, tmp_path):
    main, models, lembretes, agenda = carregar_app(monkeypatch, tmp_path)
    lembretes.enviar_email = AsyncMock(return_value=None)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica Sorriso Feliz", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte de cabelo")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    data_hora = datetime.utcnow() + timedelta(hours=2)
    _seed_agendamento(main, models, empresa, cliente, servico, data_hora)

    db = main.SessionLocal()
    try:
        asyncio.run(lembretes.enviar_lembretes_pendentes(db))
    finally:
        db.close()

    args, _ = lembretes.enviar_email.await_args
    destinatario, assunto, corpo = args
    assert destinatario == "ana@exemplo.com"
    assert "Corte de cabelo" in assunto
    assert "Ana Souza" in corpo
    assert "Clínica Sorriso Feliz" in corpo
    assert agenda.formatar_data_hora(data_hora) in corpo


def test_reagendamento_reseta_lembrete_email_enviado_em(monkeypatch, tmp_path):
    main, models, lembretes, agenda = carregar_app(monkeypatch, tmp_path)

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana Souza", email="ana@exemplo.com")
    agendamento = _seed_agendamento(
        main,
        models,
        empresa,
        cliente,
        servico,
        datetime.utcnow() + timedelta(hours=2),
        lembrete_email_enviado_em=datetime.utcnow() - timedelta(hours=1),
    )

    db = main.SessionLocal()
    try:
        agendamento_db = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
        empresa_db = db.query(models.Empresa).filter_by(id=empresa.id).first()
        novo_horario = _proxima_segunda_10h()  # sempre futuro, dentro do horário de funcionamento
        atualizado, validacao = agenda.reagendar_agendamento(db, empresa_db, agendamento_db, novo_horario)
        assert validacao.ok
        assert atualizado.lembrete_email_enviado_em is None
    finally:
        db.close()
