import asyncio
import importlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import preparar_ambiente

ENV_OVERRIDES = {"META_TOKEN": "tok-env", "META_PHONE_NUMBER_ID": "phone-env"}


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path, ENV_OVERRIDES)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    configuracoes = importlib.import_module("services.configuracoes")
    meta_client = importlib.import_module("integrations.meta_client")
    lembretes = importlib.import_module("services.lembretes")
    ai_service_module = importlib.import_module("ai.service")
    main.ensure_schema()
    return main, models, configuracoes, meta_client, lembretes, ai_service_module


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
            lembrete_canal_email=True,
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
        db.refresh(agendamento)
        return agendamento
    finally:
        db.close()


# --- obter_configuracao / atualizar_configuracao -----------------------------------


def test_obter_configuracao_cria_linha_espelhando_o_env(monkeypatch, tmp_path):
    main, models, configuracoes, *_ = carregar_app(monkeypatch, tmp_path)

    db = main.SessionLocal()
    try:
        config = configuracoes.obter_configuracao(db)
    finally:
        db.close()

    assert config.meta_token == "tok-env"
    assert config.meta_phone_number_id == "phone-env"
    assert config.ai_enabled is False

    db = main.SessionLocal()
    try:
        total = db.query(models.ConfiguracaoSistema).count()
    finally:
        db.close()
    assert total == 1


def test_obter_configuracao_e_idempotente(monkeypatch, tmp_path):
    main, models, configuracoes, *_ = carregar_app(monkeypatch, tmp_path)

    db = main.SessionLocal()
    try:
        primeira = configuracoes.obter_configuracao(db)
        segunda = configuracoes.obter_configuracao(db)
    finally:
        db.close()

    assert primeira.id == segunda.id


def test_atualizar_configuracao_persiste_mudancas(monkeypatch, tmp_path):
    main, models, configuracoes, *_ = carregar_app(monkeypatch, tmp_path)

    db = main.SessionLocal()
    try:
        configuracoes.atualizar_configuracao(db, meta_phone_number_id="novo-phone", ai_enabled=True)
    finally:
        db.close()

    db = main.SessionLocal()
    try:
        config = configuracoes.obter_configuracao(db)
        assert config.meta_phone_number_id == "novo-phone"
        assert config.ai_enabled is True
    finally:
        db.close()


def test_atualizar_configuracao_rejeita_campo_desconhecido(monkeypatch, tmp_path):
    main, models, configuracoes, *_ = carregar_app(monkeypatch, tmp_path)

    db = main.SessionLocal()
    try:
        with pytest.raises(ValueError):
            configuracoes.atualizar_configuracao(db, database_url="tentativa-de-escapar-do-escopo")
    finally:
        db.close()


def test_obter_configuracao_isolada_reflete_atualizacoes(monkeypatch, tmp_path):
    main, models, configuracoes, *_ = carregar_app(monkeypatch, tmp_path)

    db = main.SessionLocal()
    try:
        configuracoes.atualizar_configuracao(db, meta_token="token-isolado")
    finally:
        db.close()

    snapshot = configuracoes.obter_configuracao_isolada()
    assert snapshot.meta_token == "token-isolado"


def test_obter_configuracao_isolada_cai_para_env_se_banco_falhar(monkeypatch, tmp_path):
    main, models, configuracoes, *_ = carregar_app(monkeypatch, tmp_path)

    def _levanta_erro():
        raise ConnectionError("banco indisponível")

    monkeypatch.setattr(configuracoes, "SessionLocal", _levanta_erro)

    snapshot = configuracoes.obter_configuracao_isolada()
    assert snapshot.meta_token == "tok-env"
    assert snapshot.meta_phone_number_id == "phone-env"


# --- meta_client.py: URL/headers refletem configuração sem restart -----------------


class _RespostaFalsa:
    status_code = 200

    def json(self):
        return {"messages": [{"id": "wamid.1"}]}


def test_meta_client_usa_configuracao_atualizada_sem_restart(monkeypatch, tmp_path):
    main, models, configuracoes, meta_client, *_ = carregar_app(monkeypatch, tmp_path)

    urls_chamadas = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            urls_chamadas.append((url, headers.get("Authorization")))
            return _RespostaFalsa()

    with patch("integrations.meta_client.httpx.AsyncClient", FakeAsyncClient):
        asyncio.run(meta_client.enviar_botoes("5511900000001", "oi", [{"id": "menu", "titulo": "Menu"}]))

        db = main.SessionLocal()
        try:
            configuracoes.atualizar_configuracao(db, meta_phone_number_id="phone-novo", meta_token="token-novo")
        finally:
            db.close()

        asyncio.run(meta_client.enviar_botoes("5511900000001", "oi", [{"id": "menu", "titulo": "Menu"}]))

    assert len(urls_chamadas) == 2
    assert "phone-env" in urls_chamadas[0][0]
    assert urls_chamadas[0][1] == "Bearer tok-env"
    assert "phone-novo" in urls_chamadas[1][0]
    assert urls_chamadas[1][1] == "Bearer token-novo"


# --- lembretes.py: respeita antecedência vinda da configuração ---------------------


def test_lembretes_respeitam_antecedencia_configurada(monkeypatch, tmp_path):
    main, models, configuracoes, meta_client, lembretes, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    servico = _seed_servico(main, models, empresa, "Corte")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana")
    agora = datetime.utcnow()
    _seed_agendamento(main, models, empresa, cliente, servico, agora + timedelta(hours=10))

    db = main.SessionLocal()
    try:
        configuracoes.atualizar_configuracao(db, lembrete_antecedencia_horas=2)
        # com 2h de antecedência, um agendamento daqui 10h não deveria ser elegível ainda
        pendentes_curto = lembretes.buscar_agendamentos_para_lembrete(db, agora=agora)
        assert pendentes_curto == []

        configuracoes.atualizar_configuracao(db, lembrete_antecedencia_horas=24)
        pendentes_longo = lembretes.buscar_agendamentos_para_lembrete(db, agora=agora)
        assert len(pendentes_longo) == 1
    finally:
        db.close()


# --- ai/service.py: habilitar/trocar modelo via configuração -----------------------


def test_criar_ai_service_reflete_configuracao_atualizada(monkeypatch, tmp_path):
    main, models, configuracoes, meta_client, lembretes, ai_service_module = carregar_app(monkeypatch, tmp_path)

    db = main.SessionLocal()
    try:
        config_desligada = configuracoes.obter_configuracao(db)
        servico_desligado = ai_service_module.criar_ai_service(config_desligada)
        assert servico_desligado._habilitado is False

        configuracoes.atualizar_configuracao(db, ai_enabled=True, ai_api_key="chave-x", ai_model="gpt-4o")
        config_ligada = configuracoes.obter_configuracao(db)
        servico_ligado = ai_service_module.criar_ai_service(config_ligada)
        assert servico_ligado._habilitado is True
        assert servico_ligado._provider._model == "gpt-4o"
    finally:
        db.close()
