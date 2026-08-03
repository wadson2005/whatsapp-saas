import asyncio
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

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


class FakeRedis:
    def __init__(self):
        self.storage = {}

    def get(self, key):
        return self.storage.get(key)

    def set(self, key, value, ex=None):
        self.storage[key] = value

    def delete(self, key):
        self.storage.pop(key, None)

    def ping(self):
        return True


def carregar_app(monkeypatch, tmp_path: Path):
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    database_path = tmp_path / "bot-app.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    for chave, valor in BOOTSTRAP_ENV.items():
        monkeypatch.setenv(chave, valor)

    for modulo in ["main", "admin", "config", "database", "models", "schema", "conversa", "redis_client", "agenda", "meta_client", "atendimento_humano", "lembretes", "ai", "ai.provider", "ai.service", "ai.prompts", "ai.models", "ai.cache", "texto_utils", "conhecimento", "metricas"]:
        sys.modules.pop(modulo, None)

    main = importlib.import_module("main")
    conversa = importlib.import_module("conversa")
    models = importlib.import_module("models")
    main.ensure_schema()
    return main, conversa, models


def _payload_texto(instance: str, numero: str, texto: str) -> dict:
    return {
        "instance": instance,
        "data": {
            "key": {"fromMe": False, "remoteJid": numero},
            "message": {"conversation": texto},
        },
    }


def _payload_botao(instance: str, numero: str, botao_id: str, titulo: str) -> dict:
    return {
        "instance": instance,
        "data": {
            "key": {"fromMe": False, "remoteJid": numero},
            "message": {
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": botao_id, "title": titulo},
                }
            },
        },
    }


def _criar_empresa_com_agendamento(main, models, telefone: str, instancia: str):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome="Clínica Sorriso Feliz",
            slug="clinica-sorriso-feliz",
            segmento="clinica",
            telefone_whatsapp=telefone,
            evolution_instance_name=instancia,
            horario_abertura="08:00",
            horario_fechamento="18:00",
            intervalo_entre_atendimentos_minutos=15,
            ativo=True,
        )
        db.add(empresa)
        db.flush()

        servico = models.Servico(
            empresa_id=empresa.id,
            nome="Consulta inicial",
            duracao_minutos=30,
            preco=120.0,
            ativo=True,
        )
        db.add(servico)
        db.flush()

        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone)
        db.add(cliente)
        db.flush()

        agendamento = models.Agendamento(
            empresa_id=empresa.id,
            cliente_final_id=cliente.id,
            servico_id=servico.id,
            data_hora=datetime(2026, 8, 5, 14, 0),
            fim_em=datetime(2026, 8, 5, 14, 30),
            duracao_minutos=30,
            status="confirmado",
        )
        db.add(agendamento)
        db.commit()
        db.refresh(empresa)
        db.refresh(servico)
        db.refresh(agendamento)
        return empresa, servico, agendamento
    finally:
        db.close()


def test_mensagem_desconhecida_mostra_menu_e_atendente(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    _criar_empresa_com_agendamento(main, models, "5586999999999", "clinica-sorriso-feliz")

    with TestClient(main.app) as client:
        response = client.post(
            "/webhook",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999999", "preciso de ajuda"),
        )

    assert response.status_code == 200
    assert conversa.enviar_lista.await_count == 1
    args = conversa.enviar_lista.await_args.kwargs
    assert "próximo passo" in args["texto"].lower()
    ids = [linha["id"] for secao in args["secoes"] for linha in secao["linhas"]]
    assert "menu:servicos" in ids
    assert "atendimento:humano" in ids


def test_cancelamento_exige_confirmacao_e_cancela(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    _, _, agendamento = _criar_empresa_com_agendamento(main, models, "5586999999998", "clinica-sorriso-feliz")

    with TestClient(main.app) as client:
        resposta_cancelar = client.post(
            "/webhook",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999998", "cancelar agendamento"),
        )

        estado = conversa.obter_estado(agendamento.empresa_id, "5586999999998")
        assert estado["passo"] == "aguardando_cancelamento_confirmacao"
        assert conversa.enviar_botoes.await_count == 1
        botoes = conversa.enviar_botoes.await_args.kwargs["botoes"]
        assert any(botao["id"] == "cancelamento:confirmar" for botao in botoes)

        conversa.enviar_botoes.reset_mock()
        conversa.enviar_lista.reset_mock()

        resposta_confirmar = client.post(
            "/webhook",
            json=_payload_botao(
                "clinica-sorriso-feliz",
                "5586999999998",
                "cancelamento:confirmar",
                "Sim, cancelar",
            ),
        )

    assert resposta_cancelar.status_code == 200
    assert resposta_confirmar.status_code == 200
    assert conversa.enviar_botoes.await_count == 1
    assert conversa.enviar_botoes.await_args.kwargs["texto"].lower().startswith("agendamento de")

    db = main.SessionLocal()
    try:
        agendamento_atual = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
    finally:
        db.close()

    assert agendamento_atual.status == "cancelado"
    assert conversa.obter_estado(agendamento.empresa_id, "5586999999998")["passo"] == "novo"


def test_estado_inesperado_recebe_fallback_e_nao_quebra_o_contexto(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586999999997", "clinica-sorriso-feliz")
    conversa.redis_cliente.set(
        f"conversa:{empresa.id}:5586999999997",
        json.dumps({"passo": "aguardando_estado_invalido", "contexto": {"servico_id": 1}}),
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/webhook",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999997", "qualquer coisa"),
        )

    assert response.status_code == 200
    assert conversa.enviar_botoes.await_count == 1
    texto = conversa.enviar_botoes.await_args.kwargs["texto"].lower()
    assert "menu" in texto or "não entendi" in texto
    estado = conversa.obter_estado(empresa.id, "5586999999997")
    assert estado["passo"] == "aguardando_estado_invalido"


def test_ia_interpreta_cancelamento_quando_fora_das_palavras_chave(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    ai_models = importlib.import_module("ai.models")
    empresa, _, agendamento = _criar_empresa_com_agendamento(main, models, "5586999999996", "clinica-sorriso-feliz")
    conversa.redis_cliente.set(
        f"conversa:{empresa.id}:5586999999996",
        json.dumps({"passo": "agendamento_ativo", "contexto": {"agendamento_id": agendamento.id, "servico_id": agendamento.servico_id}}),
    )

    conversa.ai_service.interpretar = AsyncMock(
        return_value=ai_models.InterpretacaoIA(
            intent=ai_models.Intent.CANCELAR,
            entidades=ai_models.Entidades(),
            confianca=0.9,
            origem="ia",
        )
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/webhook",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999996", "não vou poder ir nesse horário"),
        )

    assert response.status_code == 200
    conversa.ai_service.interpretar.assert_awaited_once()
    assert conversa.enviar_botoes.await_count == 1
    botoes = conversa.enviar_botoes.await_args.kwargs["botoes"]
    assert any(botao["id"] == "cancelamento:confirmar" for botao in botoes)

    estado = conversa.obter_estado(empresa.id, "5586999999996")
    assert estado["passo"] == "aguardando_cancelamento_confirmacao"

    db = main.SessionLocal()
    try:
        atualizado = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
        assert atualizado.status != "cancelado"
    finally:
        db.close()


def test_ia_desconhecida_mantem_fallback_padrao(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    ai_models = importlib.import_module("ai.models")
    empresa, _, agendamento = _criar_empresa_com_agendamento(main, models, "5586999999995", "clinica-sorriso-feliz")
    conversa.redis_cliente.set(
        f"conversa:{empresa.id}:5586999999995",
        json.dumps({"passo": "agendamento_ativo", "contexto": {"agendamento_id": agendamento.id, "servico_id": agendamento.servico_id}}),
    )

    conversa.ai_service.interpretar = AsyncMock(
        return_value=ai_models.InterpretacaoIA(
            intent=ai_models.Intent.DESCONHECIDO,
            entidades=ai_models.Entidades(),
            confianca=0.0,
            origem="fallback",
        )
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/webhook",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999995", "posso levar meu filho junto?"),
        )

    assert response.status_code == 200
    conversa.ai_service.interpretar.assert_awaited_once()
    assert conversa.enviar_botoes.await_count == 1
    texto_resposta = conversa.enviar_botoes.await_args.kwargs["texto"].lower()
    assert "não entendi" in texto_resposta

    estado = conversa.obter_estado(empresa.id, "5586999999995")
    assert estado["passo"] == "agendamento_ativo"