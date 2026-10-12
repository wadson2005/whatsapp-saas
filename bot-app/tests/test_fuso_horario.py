import asyncio
import importlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import ADMIN_PASSWORD, preparar_ambiente

# Terça-feira, 13/10/2026, 10:00 UTC = 07:00 em Brasília = 06:00 em Manaus.
UTC_MANHA = datetime(2026, 10, 13, 10, 0)


def carregar_app(monkeypatch, tmp_path: Path, agora_utc: datetime = UTC_MANHA):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    tempo = importlib.import_module("core.tempo")
    monkeypatch.setattr(tempo, "agora_utc", lambda: agora_utc)
    main.ensure_schema()
    return main, models, tempo


def _seed_empresa(main, models, fuso: str | None = None, slug: str = "clinica-a"):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome="Clínica A",
            slug=slug,
            segmento="clinica",
            telefone_whatsapp=f"55119999{len(slug):05d}",
            evolution_instance_name=slug,
            horario_abertura="08:00",
            horario_fechamento="18:00",
            ativo=True,
            lembrete_canal_email=True,
        )
        if fuso:
            empresa.fuso_horario = fuso
        db.add(empresa)
        db.flush()
        servico = models.Servico(empresa_id=empresa.id, nome="Consulta", duracao_minutos=30, ativo=True)
        db.add(servico)
        db.commit()
        db.refresh(empresa)
        db.refresh(servico)
        return empresa, servico
    finally:
        db.close()


# --- core/tempo.py --------------------------------------------------------------


def test_agora_local_converte_para_o_fuso_da_empresa(monkeypatch, tmp_path):
    main, models, tempo = carregar_app(monkeypatch, tmp_path)

    assert tempo.agora_local() == datetime(2026, 10, 13, 7, 0)
    assert tempo.agora_local(models.Empresa(fuso_horario="America/Manaus")) == datetime(2026, 10, 13, 6, 0)
    assert tempo.agora_local(models.Empresa(fuso_horario="America/Noronha")) == datetime(2026, 10, 13, 8, 0)
    assert tempo.agora_local(models.Empresa(fuso_horario="America/Rio_Branco")) == datetime(2026, 10, 13, 5, 0)


def test_fuso_invalido_cai_para_o_padrao(monkeypatch, tmp_path):
    main, models, tempo = carregar_app(monkeypatch, tmp_path)

    assert tempo.agora_local(models.Empresa(fuso_horario="Marte/Olympus")) == datetime(2026, 10, 13, 7, 0)


def test_empresa_nova_nasce_com_fuso_de_brasilia(monkeypatch, tmp_path):
    main, models, tempo = carregar_app(monkeypatch, tmp_path)
    empresa, _ = _seed_empresa(main, models)

    assert empresa.fuso_horario == "America/Sao_Paulo"


# --- agenda: "agora" é o horário local da empresa ----------------------------------


def test_slots_de_hoje_comecam_na_abertura_local_e_nao_no_horario_utc(monkeypatch, tmp_path):
    main, models, _ = carregar_app(monkeypatch, tmp_path)
    agenda = importlib.import_module("services.agenda")
    empresa, servico = _seed_empresa(main, models)

    db = main.SessionLocal()
    try:
        slots = agenda.obter_slots_disponiveis(db, empresa, servico, limite=3)
    finally:
        db.close()

    # 07:00 em Brasília: o primeiro horário do dia (08:00) ainda está no futuro.
    assert slots[0].inicio_em == datetime(2026, 10, 13, 8, 0)


def test_horario_ja_passado_no_fuso_local_e_rejeitado(monkeypatch, tmp_path):
    # 21:00 UTC = 18:00 em Brasília: 17:00 de hoje já passou, mesmo sendo "futuro" num relógio UTC-5.
    main, models, _ = carregar_app(monkeypatch, tmp_path, agora_utc=datetime(2026, 10, 13, 21, 0))
    agenda = importlib.import_module("services.agenda")
    empresa, servico = _seed_empresa(main, models)

    db = main.SessionLocal()
    try:
        validacao = agenda.validar_agendamento(db, empresa, servico, datetime(2026, 10, 13, 17, 0))
    finally:
        db.close()

    assert not validacao.ok
    assert "já passou" in validacao.mensagem


def test_parsear_data_hora_texto_usa_a_data_local(monkeypatch, tmp_path):
    # 01:00 UTC de 14/10 ainda é 13/10 (22:00) em Brasília: "amanhã" é 14/10.
    carregar_app(monkeypatch, tmp_path, agora_utc=datetime(2026, 10, 14, 1, 0))
    agenda = importlib.import_module("services.agenda")

    assert agenda.parsear_data_hora_texto("amanhã às 10h") == datetime(2026, 10, 14, 10, 0)


def test_parsear_data_hora_texto_impossivel_retorna_none(monkeypatch, tmp_path):
    carregar_app(monkeypatch, tmp_path)
    agenda = importlib.import_module("services.agenda")

    assert agenda.parsear_data_hora_texto("31/02 às 10h") is None
    assert agenda.parsear_data_hora_texto("hoje às 25h") is None
    assert agenda.parsear_data_hora_texto("10/13 10:00") is None


# --- conversa: horário de resposta do bot ----------------------------------------


def _empresa_horario_resposta(models, fuso: str = "America/Sao_Paulo"):
    return models.Empresa(
        fuso_horario=fuso,
        atendimento_automatico_ativo=True,
        horario_resposta_inicio="08:00",
        horario_resposta_fim="18:00",
        dias_funcionamento="0,1,2,3,4,5",
    )


def test_bot_respeita_horario_de_resposta_no_fuso_da_empresa(monkeypatch, tmp_path):
    # 20:30 UTC = 17:30 em Brasília (dentro do horário) = 16:30 em Manaus.
    main, models, _ = carregar_app(monkeypatch, tmp_path, agora_utc=datetime(2026, 10, 13, 20, 30))
    conversa = importlib.import_module("conversa")

    assert conversa._empresa_horario_disponivel(_empresa_horario_resposta(models)) is True
    assert conversa._empresa_horario_disponivel(_empresa_horario_resposta(models, "America/Manaus")) is True


def test_bot_fora_do_horario_local_mesmo_dentro_do_horario_utc(monkeypatch, tmp_path):
    # 10:00 UTC estaria "dentro" de 08:00–18:00 num servidor em UTC, mas são 07:00 em Brasília.
    main, models, _ = carregar_app(monkeypatch, tmp_path)
    conversa = importlib.import_module("conversa")

    assert conversa._empresa_horario_disponivel(_empresa_horario_resposta(models)) is False


# --- lembretes: janela calculada no horário local de cada empresa ------------------


def _seed_agendamento(main, models, empresa, servico, data_hora: datetime):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(
            empresa_id=empresa.id, telefone=f"55119000{empresa.id:05d}", nome="Ana", email="ana@exemplo.com"
        )
        db.add(cliente)
        db.flush()
        db.add(
            models.Agendamento(
                empresa_id=empresa.id,
                cliente_final_id=cliente.id,
                servico_id=servico.id,
                data_hora=data_hora,
                duracao_minutos=30,
                status="agendado",
            )
        )
        db.commit()
    finally:
        db.close()


def test_lembrete_considera_o_fuso_de_cada_empresa(monkeypatch, tmp_path):
    main, models, _ = carregar_app(monkeypatch, tmp_path)
    lembretes = importlib.import_module("services.lembretes")
    monkeypatch.setattr(lembretes, "agora_utc", lambda: UTC_MANHA)
    lembretes.enviar_email = AsyncMock(return_value=None)

    # 06:30 local: já passou em Brasília (07:00), ainda é futuro em Manaus (06:00).
    empresa_sp, servico_sp = _seed_empresa(main, models, slug="clinica-sp")
    empresa_am, servico_am = _seed_empresa(main, models, fuso="America/Manaus", slug="clinica-manaus")
    _seed_agendamento(main, models, empresa_sp, servico_sp, datetime(2026, 10, 13, 6, 30))
    _seed_agendamento(main, models, empresa_am, servico_am, datetime(2026, 10, 13, 6, 30))

    db = main.SessionLocal()
    try:
        pendentes = lembretes.buscar_agendamentos_para_lembrete(db)
        assert [agendamento.empresa_id for agendamento in pendentes] == [empresa_am.id]
        assert asyncio.run(lembretes.enviar_lembretes_pendentes(db)) == 1
    finally:
        db.close()


def test_lembrete_fora_da_antecedencia_local_nao_e_enviado(monkeypatch, tmp_path):
    main, models, _ = carregar_app(monkeypatch, tmp_path)
    lembretes = importlib.import_module("services.lembretes")
    monkeypatch.setattr(lembretes, "agora_utc", lambda: UTC_MANHA)
    empresa, servico = _seed_empresa(main, models)
    # Antecedência padrão de 24h a partir de 07:00 local: 08:00 do dia seguinte já está fora.
    _seed_agendamento(main, models, empresa, servico, datetime(2026, 10, 13, 7, 0) + timedelta(hours=25))

    db = main.SessionLocal()
    try:
        assert lembretes.buscar_agendamentos_para_lembrete(db) == []
    finally:
        db.close()


# --- painel: escolha do fuso em /admin/configurar-bot/horarios -------------------


def test_painel_salva_fuso_valido_e_ignora_invalido(monkeypatch, tmp_path):
    main, models, _ = carregar_app(monkeypatch, tmp_path)
    empresa, _ = _seed_empresa(main, models)

    with TestClient(main.app, base_url="https://testserver") as client:
        login = client.post(
            "/admin/login", data={"username": "admin", "password": ADMIN_PASSWORD}, follow_redirects=False
        )
        assert login.status_code == 303

        url = f"/admin/configurar-bot/horarios?empresa_id={empresa.id}"
        pagina = client.get(url)
        assert pagina.status_code == 200
        assert "America/Manaus" in pagina.text

        assert client.post(url, data={"fuso_horario": "America/Manaus"}, follow_redirects=False).status_code == 303
        assert client.post(url, data={"fuso_horario": "Europe/Paris"}, follow_redirects=False).status_code == 303

    db = main.SessionLocal()
    try:
        assert db.get(models.Empresa, empresa.id).fuso_horario == "America/Manaus"
    finally:
        db.close()
