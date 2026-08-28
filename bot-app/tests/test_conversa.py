import asyncio
import importlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from conftest import WEBHOOK_SECRET, FakeRedis, preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    conversa = importlib.import_module("conversa")
    models = importlib.import_module("core.models")
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


def _criar_empresa_com_agendamento(main, models, telefone: str, instancia: str, slug: str = "clinica-sorriso-feliz"):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome="Clínica Sorriso Feliz",
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


# --- cada empresa responde só pela própria instância Evolution --------------------


def test_cada_empresa_responde_exclusivamente_pela_propria_instancia(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    empresa_a, _, _ = _criar_empresa_com_agendamento(main, models, "5586999999901", "instancia-empresa-a", slug="empresa-a")
    empresa_b, _, _ = _criar_empresa_com_agendamento(main, models, "5586999999902", "instancia-empresa-b", slug="empresa-b")

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta_a = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("instancia-empresa-a", "5586999999801", "oibot"),
        )
        resposta_b = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("instancia-empresa-b", "5586999999802", "oibot"),
        )

    assert resposta_a.status_code == 200
    assert resposta_b.status_code == 200
    assert conversa.enviar_lista.await_count == 2

    instancias_usadas = [chamada.kwargs["instance"] for chamada in conversa.enviar_lista.await_args_list]
    assert instancias_usadas == ["instancia-empresa-a", "instancia-empresa-b"]
    # nenhuma das duas usou a instância da outra empresa, nem uma global/pessoal
    assert "instancia-empresa-a" in instancias_usadas
    assert "instancia-empresa-b" in instancias_usadas


def test_empresa_sem_instancia_evolution_falha_de_forma_explicita_ao_responder(monkeypatch, tmp_path):
    """Nunca cai pra um número/instância global — antes de enviar, tem que existir
    a instância da própria empresa. Sem ela, o envio real (evolution_client, não
    mockado neste teste) recusa explicitamente, em vez de mandar por outro canal."""
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    evolution_client = importlib.import_module("integrations.evolution_client")

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586999999903", "instancia-empresa-c")
    db = main.SessionLocal()
    try:
        empresa_db = db.query(models.Empresa).filter_by(id=empresa.id).first()
        empresa_db.evolution_instance_name = None
        db.commit()
        db.refresh(empresa_db)
    finally:
        db.close()

    db = main.SessionLocal()
    try:
        empresa_sem_instancia = db.query(models.Empresa).filter_by(id=empresa.id).first()
        with pytest.raises(evolution_client.InstanciaNaoConfiguradaError):
            asyncio.run(conversa.processar_mensagem(db, empresa_sem_instancia, "5586999999803", "oibot", None))
    finally:
        db.close()


def test_mensagem_desconhecida_mostra_menu_e_atendente(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    _criar_empresa_com_agendamento(main, models, "5586999999999", "clinica-sorriso-feliz")

    with TestClient(main.app, base_url="https://testserver") as client:
        response = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999999", "preciso de ajuda"),
        )

    assert response.status_code == 200
    assert conversa.enviar_lista.await_count == 1
    args = conversa.enviar_lista.await_args.kwargs
    assert "próximo passo" in args["texto"].lower()
    ids = [linha["id"] for secao in args["secoes"] for linha in secao["linhas"]]
    assert "menu:servicos" in ids
    assert "atendimento:humano" in ids


def test_palavra_de_ativacao_customizada_da_empresa_abre_direto_na_lista_de_servicos(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586999999997", "clinica-sorriso-feliz")
    db = main.SessionLocal()
    try:
        empresa_db = db.query(models.Empresa).filter_by(id=empresa.id).first()
        empresa_db.palavra_ativacao = "quero marcar"
        db.commit()
    finally:
        db.close()

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999997", "quero marcar"),
        )

    assert resposta.status_code == 200
    assert conversa.enviar_lista.await_count == 1
    args = conversa.enviar_lista.await_args.kwargs
    assert "escolha um serviço" in args["texto"].lower()


def test_palavra_de_ativacao_padrao_para_de_funcionar_apos_empresa_customizar_a_sua(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586999999996", "clinica-sorriso-feliz")
    db = main.SessionLocal()
    try:
        empresa_db = db.query(models.Empresa).filter_by(id=empresa.id).first()
        empresa_db.palavra_ativacao = "quero marcar"
        db.commit()
    finally:
        db.close()

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999996", "oibot"),
        )

    assert resposta.status_code == 200
    assert conversa.enviar_lista.await_count == 1
    args = conversa.enviar_lista.await_args.kwargs
    # "oibot" (palavra global antiga) não é mais gatilho pra essa empresa — cai no menu principal
    assert "próximo passo" in args["texto"].lower()


def test_cancelamento_exige_confirmacao_e_cancela(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    _, _, agendamento = _criar_empresa_com_agendamento(main, models, "5586999999998", "clinica-sorriso-feliz")

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta_cancelar = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
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
            f"/webhook?token={WEBHOOK_SECRET}",
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

    with TestClient(main.app, base_url="https://testserver") as client:
        response = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
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

    fake_ia = SimpleNamespace(
        interpretar=AsyncMock(
            return_value=ai_models.InterpretacaoIA(
                intent=ai_models.Intent.CANCELAR,
                entidades=ai_models.Entidades(),
                confianca=0.9,
                origem="ia",
            )
        )
    )
    conversa.criar_ai_service = lambda config: fake_ia

    with TestClient(main.app, base_url="https://testserver") as client:
        response = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999996", "não vou poder ir nesse horário"),
        )

    assert response.status_code == 200
    fake_ia.interpretar.assert_awaited_once()
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

    with TestClient(main.app, base_url="https://testserver") as client:
        response = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999995", "posso levar meu filho junto?"),
        )

    assert response.status_code == 200
    fake_ia.interpretar.assert_awaited_once()
    assert conversa.enviar_botoes.await_count == 1
    texto_resposta = conversa.enviar_botoes.await_args.kwargs["texto"].lower()
    assert "não entendi" in texto_resposta

    estado = conversa.obter_estado(empresa.id, "5586999999995")
    assert estado["passo"] == "agendamento_ativo"


def test_configuracao_pelo_painel_ativa_ia_sem_reiniciar_processo(monkeypatch, tmp_path):
    """Prova de ponta a ponta: ativar a IA em /admin/configuracoes vale na mensagem
    seguinte, dentro do mesmo processo — sem mockar conversa.criar_ai_service, para
    exercitar de verdade a cadeia painel -> banco -> criar_ai_service -> OpenAIProvider."""
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    ai_provider_module = importlib.import_module("ai.provider")
    ai_cache_module = importlib.import_module("ai.cache")
    ai_cache_module.redis_cliente = FakeRedis()  # isola do Redis real — senão um cache de execução anterior mascara o teste
    chamadas = []

    class FakeOpenAIProvider:
        def __init__(self, api_key, model, timeout_segundos):
            self.api_key = api_key

        async def completar(self, mensagens):
            chamadas.append(mensagens)
            return json.dumps({"intent": "falar_com_atendente", "entidades": {}, "confianca": 0.9})

    monkeypatch.setattr(ai_provider_module, "OpenAIProvider", FakeOpenAIProvider)

    empresa, _, agendamento = _criar_empresa_com_agendamento(main, models, "5586999999994", "clinica-sorriso-feliz")
    conversa.redis_cliente.set(
        f"conversa:{empresa.id}:5586999999994",
        json.dumps({"passo": "agendamento_ativo", "contexto": {"agendamento_id": agendamento.id, "servico_id": agendamento.servico_id}}),
    )

    with TestClient(main.app, base_url="https://testserver") as client:
        login = client.post(
            "/admin/login",
            data={"username": "admin", "password": "senha-super-segura-123"},
            follow_redirects=False,
        )
        assert login.status_code == 303

        resposta_config = client.post(
            "/admin/configuracoes",
            data={
                "meta_phone_number_id": "x",
                "lembrete_antecedencia_horas": "24",
                "lembrete_intervalo_minutos": "15",
                "ai_enabled": "on",
                "ai_provider": "openai",
                "ai_model": "gpt-4o-mini",
                "ai_timeout_segundos": "6",
                "ai_cache_ttl_segundos": "600",
                "ai_api_key": "chave-de-teste",
            },
            follow_redirects=False,
        )
        assert resposta_config.status_code == 303

        response = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999994", "queria falar com alguém sobre um caso específico"),
        )

    assert response.status_code == 200
    assert len(chamadas) == 1, "a IA deveria ter sido chamada de verdade, refletindo a configuração salva no painel"