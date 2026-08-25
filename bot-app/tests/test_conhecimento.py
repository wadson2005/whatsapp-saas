import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import WEBHOOK_SECRET, FakeRedis, preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    conhecimento = importlib.import_module("services.conhecimento")
    conversa = importlib.import_module("conversa")
    main.ensure_schema()
    return main, models, conhecimento, conversa


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


def _login_admin(client: TestClient):
    resposta = client.post(
        "/admin/login",
        data={"username": "admin", "password": "senha-super-segura-123"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303


def test_criar_e_listar_conhecimento(monkeypatch, tmp_path):
    main, models, conhecimento, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")

    db = main.SessionLocal()
    try:
        conhecimento.criar_conhecimento(db, empresa.id, "Convênios", "Aceita Unimed?", "Sim, atendemos Unimed.")
        entradas = conhecimento.listar_conhecimento(db, empresa.id)
    finally:
        db.close()

    assert len(entradas) == 1
    assert entradas[0].pergunta == "Aceita Unimed?"
    assert entradas[0].ativo is True


def test_atualizar_conhecimento(monkeypatch, tmp_path):
    main, models, conhecimento, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")

    db = main.SessionLocal()
    try:
        entrada = conhecimento.criar_conhecimento(db, empresa.id, None, "Tem estacionamento?", "Sim, gratuito.")
        entrada_id = entrada.id
        conhecimento.atualizar_conhecimento(entrada, "Estrutura", "Tem estacionamento?", "Sim, gratuito e coberto.", True)
        db.commit()
    finally:
        db.close()

    db = main.SessionLocal()
    try:
        atualizado = db.query(models.EmpresaConhecimento).filter_by(id=entrada_id).first()
        assert atualizado.categoria == "Estrutura"
        assert atualizado.resposta == "Sim, gratuito e coberto."
    finally:
        db.close()


def test_excluir_conhecimento_e_nao_aparece_mais(monkeypatch, tmp_path):
    main, models, conhecimento, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")

    db = main.SessionLocal()
    try:
        entrada = conhecimento.criar_conhecimento(db, empresa.id, None, "Tem wifi?", "Sim.")
        conhecimento.excluir_conhecimento(entrada)
        db.commit()
        entradas = conhecimento.listar_conhecimento(db, empresa.id)
    finally:
        db.close()

    assert entradas == []


def test_buscar_resposta_encontra_pergunta_similar(monkeypatch, tmp_path):
    main, models, conhecimento, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")

    db = main.SessionLocal()
    try:
        conhecimento.criar_conhecimento(db, empresa.id, "Convênios", "Aceita Unimed?", "Sim, atendemos Unimed.")
        encontrada = conhecimento.buscar_resposta(db, empresa.id, "vocês aceitam Unimed?")
    finally:
        db.close()

    assert encontrada is not None
    assert encontrada.resposta == "Sim, atendemos Unimed."


def test_buscar_resposta_nao_encontra_quando_nao_relacionada(monkeypatch, tmp_path):
    main, models, conhecimento, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")

    db = main.SessionLocal()
    try:
        conhecimento.criar_conhecimento(db, empresa.id, "Convênios", "Aceita Unimed?", "Sim, atendemos Unimed.")
        encontrada = conhecimento.buscar_resposta(db, empresa.id, "qual o horário de vocês amanhã")
    finally:
        db.close()

    assert encontrada is None


def test_buscar_resposta_ignora_entrada_inativa(monkeypatch, tmp_path):
    main, models, conhecimento, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")

    db = main.SessionLocal()
    try:
        entrada = conhecimento.criar_conhecimento(db, empresa.id, None, "Tem estacionamento?", "Sim, gratuito.")
        entrada.ativo = False
        db.commit()
        encontrada = conhecimento.buscar_resposta(db, empresa.id, "tem estacionamento gratuito?")
    finally:
        db.close()

    assert encontrada is None


def test_admin_crud_http(monkeypatch, tmp_path):
    main, models, conhecimento, _ = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_admin(client)

        resposta_criar = client.post(
            "/admin/conhecimento/novo",
            data={
                "empresa_id": str(empresa.id),
                "categoria": "Horários",
                "pergunta": "Qual o horário de funcionamento?",
                "resposta": "Segunda a sexta das 8h às 18h.",
                "ativo": "on",
            },
            follow_redirects=False,
        )
        assert resposta_criar.status_code == 303

        resposta_lista = client.get(f"/admin/conhecimento?empresa_id={empresa.id}")
        assert "Qual o horário de funcionamento?" in resposta_lista.text

        db = main.SessionLocal()
        try:
            entrada = db.query(models.EmpresaConhecimento).filter_by(empresa_id=empresa.id).first()
        finally:
            db.close()

        resposta_toggle = client.post(f"/admin/conhecimento/{entrada.id}/toggle", follow_redirects=False)
        assert resposta_toggle.status_code == 303

        db = main.SessionLocal()
        try:
            atualizado = db.query(models.EmpresaConhecimento).filter_by(id=entrada.id).first()
            assert atualizado.ativo is False
        finally:
            db.close()

        resposta_excluir = client.post(f"/admin/conhecimento/{entrada.id}/excluir", follow_redirects=False)
        assert resposta_excluir.status_code == 303

        resposta_lista_depois = client.get(f"/admin/conhecimento?empresa_id={empresa.id}")
        assert "Qual o horário de funcionamento?" not in resposta_lista_depois.text


def _payload_texto(instance: str, numero: str, texto: str) -> dict:
    return {
        "instance": instance,
        "data": {
            "key": {"fromMe": False, "remoteJid": numero},
            "message": {"conversation": texto},
        },
    }


def test_conversa_usa_conhecimento_antes_da_ia(monkeypatch, tmp_path):
    main, models, conhecimento, conversa = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()
    fake_ia = SimpleNamespace(interpretar=AsyncMock())
    conversa.criar_ai_service = lambda config: fake_ia

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    db = main.SessionLocal()
    try:
        conhecimento.criar_conhecimento(db, empresa.id, "Estrutura", "Tem estacionamento?", "Sim, gratuito.")
    finally:
        db.close()

    conversa.redis_cliente.set(
        f"conversa:{empresa.id}:5511900000001",
        json.dumps({"passo": "agendamento_ativo", "contexto": {}}),
    )

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("instancia-a", "5511900000001", "vocês têm estacionamento gratuito?"),
        )

    assert resposta.status_code == 200
    fake_ia.interpretar.assert_not_awaited()
    assert conversa.enviar_botoes.await_count == 1
    assert conversa.enviar_botoes.await_args.kwargs["texto"] == "Sim, gratuito."


def test_conversa_sem_match_aciona_ia_normalmente(monkeypatch, tmp_path):
    main, models, conhecimento, conversa = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    ai_models = importlib.import_module("ai.models")
    fake_ia = SimpleNamespace(
        interpretar=AsyncMock(
            return_value=ai_models.InterpretacaoIA(
                intent=ai_models.Intent.DESCONHECIDO,
                entidades=ai_models.Entidades(),
                confianca=0.0,
                origem="fallback",
            )
        )
    )
    conversa.criar_ai_service = lambda config: fake_ia

    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    db = main.SessionLocal()
    try:
        conhecimento.criar_conhecimento(db, empresa.id, "Estrutura", "Tem estacionamento?", "Sim, gratuito.")
    finally:
        db.close()

    conversa.redis_cliente.set(
        f"conversa:{empresa.id}:5511900000002",
        json.dumps({"passo": "agendamento_ativo", "contexto": {}}),
    )

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("instancia-a", "5511900000002", "posso levar meu cachorro junto?"),
        )

    assert resposta.status_code == 200
    fake_ia.interpretar.assert_awaited_once()
