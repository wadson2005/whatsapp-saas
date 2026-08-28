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
    conversa.enviar_texto = AsyncMock()

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
    assert conversa.enviar_texto.await_count == 2

    instancias_usadas = [chamada.kwargs["instance"] for chamada in conversa.enviar_texto.await_args_list]
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
    conversa.enviar_texto = AsyncMock()

    _criar_empresa_com_agendamento(main, models, "5586999999999", "clinica-sorriso-feliz")

    with TestClient(main.app, base_url="https://testserver") as client:
        response = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999999", "preciso de ajuda"),
        )

    assert response.status_code == 200
    assert conversa.enviar_texto.await_count == 1
    args = conversa.enviar_texto.await_args.kwargs
    texto = args["texto"].lower()
    assert "próximo passo" in texto
    assert "ver serviços" in texto
    assert "falar com atendente" in texto


def test_palavra_de_ativacao_customizada_da_empresa_abre_direto_na_lista_de_servicos(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

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
    assert conversa.enviar_texto.await_count == 1
    args = conversa.enviar_texto.await_args.kwargs
    assert "escolha um serviço" in args["texto"].lower()


def test_palavra_de_ativacao_padrao_para_de_funcionar_apos_empresa_customizar_a_sua(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

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
    assert conversa.enviar_texto.await_count == 1
    args = conversa.enviar_texto.await_args.kwargs
    # "oibot" (palavra global antiga) não é mais gatilho pra essa empresa — cai no menu principal
    assert "próximo passo" in args["texto"].lower()


def test_cancelamento_exige_confirmacao_e_cancela(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

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
        conversa.enviar_texto.reset_mock()

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
    conversa.enviar_texto = AsyncMock()

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
    conversa.enviar_texto = AsyncMock()

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
    conversa.enviar_texto = AsyncMock()

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
    conversa.enviar_texto = AsyncMock()

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


# --- seleção numérica de opções (substitui a necessidade de tocar numa lista) ----


def test_resolver_selecao_numerica_casos_basicos(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)

    # número válido -> resolve pro id correspondente
    assert conversa._resolver_selecao_numerica("2", {"_opcoes": ["a", "b", "c"]}) == ("b", True)
    # texto (não-número) -> não interfere, segue o reconhecimento textual normal
    assert conversa._resolver_selecao_numerica("b", {"_opcoes": ["a", "b", "c"]}) == (None, False)
    # número fora do intervalo mostrado -> inválido, mas sinalizado pra avisar o cliente
    assert conversa._resolver_selecao_numerica("9", {"_opcoes": ["a", "b", "c"]}) == (None, True)
    # mesmo número, contexto sem opções ativas (passo diferente) -> não é tratado como seleção
    assert conversa._resolver_selecao_numerica("2", {}) == (None, False)


def test_resolver_selecao_numerica_normaliza_espacos_e_zeros_a_esquerda(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    contexto = {"_opcoes": ["a", "b", "c"]}

    # variações razoáveis do mesmo número continuam resolvendo pra mesma opção
    for entrada in ["1", " 1 ", "01", " 01 ", "  1"]:
        assert conversa._resolver_selecao_numerica(entrada, contexto) == ("a", True), entrada

    # formatos que NÃO devem ser tratados como seleção numérica (seguem pro
    # reconhecimento textual normal, sem o aviso de "opção inválida")
    for entrada in ["1.", "opção 1", "+1", "-1", "1 2"]:
        assert conversa._resolver_selecao_numerica(entrada, contexto) == (None, False), entrada


def test_resolver_selecao_numerica_nao_quebra_com_digito_unicode_nao_decimal(monkeypatch, tmp_path):
    """Regressão: str.isdigit() é True para caracteres como '²' (superscript) ou
    '½' (fração), mas int() não consegue convertê-los — um ValueError não tratado
    aqui derrubaria o webhook inteiro com 500 se um cliente mandasse um desses
    caracteres (o mesmo tipo de incidente que motivou trocar o enviar_lista)."""
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    contexto = {"_opcoes": ["a", "b", "c"]}

    assert conversa._resolver_selecao_numerica("²", contexto) == (None, False)
    assert conversa._resolver_selecao_numerica("½", contexto) == (None, False)


def test_resolver_selecao_numerica_com_lista_grande_multiplos_digitos(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    opcoes = [f"item:{i}" for i in range(1, 31)]  # 30 opções
    contexto = {"_opcoes": opcoes}

    assert conversa._resolver_selecao_numerica("9", contexto) == ("item:9", True)
    assert conversa._resolver_selecao_numerica("10", contexto) == ("item:10", True)
    assert conversa._resolver_selecao_numerica("11", contexto) == ("item:11", True)
    assert conversa._resolver_selecao_numerica("20", contexto) == ("item:20", True)
    assert conversa._resolver_selecao_numerica("30", contexto) == ("item:30", True)
    assert conversa._resolver_selecao_numerica("31", contexto) == (None, True)  # fora do intervalo
    assert conversa._resolver_selecao_numerica("0", contexto) == (None, True)


def test_menu_principal_permite_selecionar_por_numero(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586977770010", "clinica-menu-numero")
    numero_cliente = "5586977779910"  # cliente sem agendamento prévio -> menu genérico

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta_menu = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-menu-numero", numero_cliente, "preciso de ajuda"),
        )
        assert resposta_menu.status_code == 200
        texto_menu = conversa.enviar_texto.await_args.kwargs["texto"]
        assert "1. Ver serviços" in texto_menu
        assert "Digite o número da opção" in texto_menu

        estado_menu = conversa.obter_estado(empresa.id, numero_cliente)
        assert estado_menu["passo"] == "menu_principal"
        # o número 1 tem que representar exatamente a primeira opção exibida
        assert estado_menu["contexto"]["_opcoes"][0] == "menu:servicos"

        conversa.enviar_texto.reset_mock()
        resposta_numero = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-menu-numero", numero_cliente, "1"),
        )

    assert resposta_numero.status_code == 200
    assert conversa.enviar_texto.await_count == 1
    assert "escolha um serviço" in conversa.enviar_texto.await_args.kwargs["texto"].lower()
    assert conversa.obter_estado(empresa.id, numero_cliente)["passo"] == "aguardando_servico"


def test_numero_fora_do_intervalo_mantem_contexto_e_depois_aceita_numero_valido(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586977770011", "clinica-menu-invalido")
    numero_cliente = "5586977779911"

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-menu-invalido", numero_cliente, "preciso de ajuda"))
        estado_antes = conversa.obter_estado(empresa.id, numero_cliente)

        conversa.enviar_texto.reset_mock()
        resposta_invalida = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-menu-invalido", numero_cliente, "99"),
        )

        assert resposta_invalida.status_code == 200
        assert conversa.enviar_texto.await_count == 1
        texto_invalido = conversa.enviar_texto.await_args.kwargs["texto"].lower()
        assert "não encontrei essa opção" in texto_invalido
        assert "1 a 4" in texto_invalido  # 4 atalhos: serviços, reagendar, cancelar, atendente

        # nem avançou de passo, nem perdeu o mapeamento número -> opção
        estado_depois = conversa.obter_estado(empresa.id, numero_cliente)
        assert estado_depois == estado_antes

        conversa.enviar_texto.reset_mock()
        resposta_valida = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-menu-invalido", numero_cliente, "1"),
        )

    assert resposta_valida.status_code == 200
    assert conversa.obter_estado(empresa.id, numero_cliente)["passo"] == "aguardando_servico"


def _substituir_servicos(main, models, empresa_id: int) -> dict[str, int]:
    """Troca os serviços da empresa por Corte/Barba/Corte + Barba, nessa ordem
    (ordem_exibicao explícita), pra reproduzir o exemplo do critério de sucesso."""
    db = main.SessionLocal()
    try:
        db.query(models.Servico).filter_by(empresa_id=empresa_id).delete()
        nomes = [("Corte", 30, 50.0), ("Barba", 20, 30.0), ("Corte + Barba", 45, 70.0)]
        ids = {}
        for ordem, (nome, duracao, preco) in enumerate(nomes, start=1):
            servico = models.Servico(
                empresa_id=empresa_id, nome=nome, duracao_minutos=duracao, preco=preco,
                ordem_exibicao=ordem, ativo=True,
            )
            db.add(servico)
            db.flush()
            ids[nome] = servico.id
        db.commit()
        return ids
    finally:
        db.close()


def test_selecao_de_servico_por_numero_e_por_texto(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586977770012", "clinica-servicos-numero")
    ids_servicos = _substituir_servicos(main, models, empresa.id)

    with TestClient(main.app, base_url="https://testserver") as client:
        # cliente 1: escolhe o serviço "2" (Barba) por número
        numero_1 = "5586977779912"
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-servicos-numero", numero_1, "ver serviços"))
        conversa.enviar_botoes.reset_mock()
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-servicos-numero", numero_1, "2"))

        assert "Barba" in conversa.enviar_botoes.await_args.kwargs["texto"]
        estado_1 = conversa.obter_estado(empresa.id, numero_1)
        assert estado_1["passo"] == "aguardando_periodo"
        assert estado_1["contexto"]["servico_id"] == ids_servicos["Barba"]

        # cliente 2: escolhe o mesmo serviço digitando o nome -> reconhecimento textual preservado
        numero_2 = "5586977779913"
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-servicos-numero", numero_2, "ver serviços"))
        conversa.enviar_botoes.reset_mock()
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-servicos-numero", numero_2, "Barba"))

    estado_2 = conversa.obter_estado(empresa.id, numero_2)
    assert estado_2["passo"] == "aguardando_periodo"
    assert estado_2["contexto"]["servico_id"] == ids_servicos["Barba"]


def test_selecao_de_horario_por_numero_agenda_o_slot_correto(monkeypatch, tmp_path):
    """Reproduz o critério de sucesso: menu -> serviço -> horário, tudo por número."""
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586977770013", "clinica-horario-numero")
    _substituir_servicos(main, models, empresa.id)
    numero_cliente = "5586977779914"

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-horario-numero", numero_cliente, "preciso de ajuda"))
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-horario-numero", numero_cliente, "1"))  # ver serviços
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-horario-numero", numero_cliente, "2"))  # Barba

        client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_botao("clinica-horario-numero", numero_cliente, "periodo:manha", "Manhã"),
        )

        estado_slots = conversa.obter_estado(empresa.id, numero_cliente)
        assert estado_slots["passo"] == "aguardando_slot"
        segunda_opcao_id = estado_slots["contexto"]["_opcoes"][1]
        segundo_horario = conversa._id_slot_para_datetime(segunda_opcao_id)

        resposta_final = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-horario-numero", numero_cliente, "2"),
        )

    assert resposta_final.status_code == 200
    db = main.SessionLocal()
    try:
        agendamento_criado = (
            db.query(models.Agendamento)
            .join(models.ClienteFinal)
            .filter(models.ClienteFinal.telefone == numero_cliente, models.Agendamento.empresa_id == empresa.id)
            .order_by(models.Agendamento.id.desc())
            .first()
        )
    finally:
        db.close()

    assert agendamento_criado is not None
    assert agendamento_criado.data_hora == segundo_horario
    assert conversa.obter_estado(empresa.id, numero_cliente)["passo"] == "agendamento_ativo"


def test_sugestoes_de_horario_texto_livre_viram_lista_numerada_selecionavel(monkeypatch, tmp_path):
    """Cobre o ramo de sugestões de `_horario_texto_livre` (ponto 5 dos 7
    identificados): cliente digita uma data já passada em texto livre, o bot
    responde com sugestões futuras, e essas sugestões precisam ser selecionáveis
    por número (passo muda pra aguardando_slot, como o resto das listas de horário)."""
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

    empresa, _, _ = _criar_empresa_com_agendamento(main, models, "5586977770015", "clinica-sugestoes-texto")
    numero_cliente = "5586977779915"

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-sugestoes-texto", numero_cliente, "ver serviços"))
        client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload_texto("clinica-sugestoes-texto", numero_cliente, "1"))  # Consulta inicial
        client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_botao("clinica-sugestoes-texto", numero_cliente, "periodo:outro", "Prefiro digitar"),
        )
        assert conversa.obter_estado(empresa.id, numero_cliente)["passo"] == "aguardando_horario_texto"

        # data já passada -> validar_agendamento recusa e sugere horários futuros
        client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sugestoes-texto", numero_cliente, "01/01/2020 10:00"),
        )

        estado_sugestoes = conversa.obter_estado(empresa.id, numero_cliente)
        assert estado_sugestoes["passo"] == "aguardando_slot"
        opcoes = estado_sugestoes["contexto"]["_opcoes"]
        assert len(opcoes) > 0
        primeiro_horario = conversa._id_slot_para_datetime(opcoes[0])

        resposta = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sugestoes-texto", numero_cliente, "1"),
        )

    assert resposta.status_code == 200
    db = main.SessionLocal()
    try:
        agendamento_criado = (
            db.query(models.Agendamento)
            .join(models.ClienteFinal)
            .filter(models.ClienteFinal.telefone == numero_cliente, models.Agendamento.empresa_id == empresa.id)
            .order_by(models.Agendamento.id.desc())
            .first()
        )
    finally:
        db.close()

    assert agendamento_criado is not None
    assert agendamento_criado.data_hora == primeiro_horario


def test_sugestoes_de_reagendamento_apos_conflito_sao_selecionaveis_por_numero(monkeypatch, tmp_path):
    """Cobre o ramo de sugestões de `_reagendar_slot_escolhido` (ponto 7 dos 7
    identificados): tentativa de reagendar pra uma data já passada gera
    sugestões futuras, que precisam continuar selecionáveis por número."""
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

    numero_cliente = "5586977770016"
    empresa, _, agendamento = _criar_empresa_com_agendamento(main, models, numero_cliente, "clinica-reagendar-sugestoes")

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-reagendar-sugestoes", numero_cliente, "reagendar"),
        )
        assert conversa.obter_estado(empresa.id, numero_cliente)["passo"] == "aguardando_reagendamento_slot"

        # data já passada -> reagendar_agendamento recusa e sugere horários futuros
        client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-reagendar-sugestoes", numero_cliente, "01/01/2020 10:00"),
        )

        estado_sugestoes = conversa.obter_estado(empresa.id, numero_cliente)
        assert estado_sugestoes["passo"] == "aguardando_reagendamento_slot"
        opcoes = estado_sugestoes["contexto"]["_opcoes"]
        assert len(opcoes) > 0
        primeiro_horario = conversa._id_slot_para_datetime(opcoes[0])

        resposta = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-reagendar-sugestoes", numero_cliente, "1"),
        )

    assert resposta.status_code == 200
    db = main.SessionLocal()
    try:
        atualizado = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
    finally:
        db.close()

    assert atualizado.status == "agendado"
    assert atualizado.data_hora == primeiro_horario


def test_reagendamento_permite_selecionar_novo_horario_por_numero(monkeypatch, tmp_path):
    main, conversa, models = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_texto = AsyncMock()

    numero_cliente = "5586977770014"  # mesmo telefone do agendamento pré-existente criado abaixo
    empresa, _, agendamento = _criar_empresa_com_agendamento(main, models, numero_cliente, "clinica-reagendar-numero")

    with TestClient(main.app, base_url="https://testserver") as client:
        client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-reagendar-numero", numero_cliente, "reagendar"),
        )

        estado_slots = conversa.obter_estado(empresa.id, numero_cliente)
        assert estado_slots["passo"] == "aguardando_reagendamento_slot"
        segunda_opcao_id = estado_slots["contexto"]["_opcoes"][1]
        segundo_horario = conversa._id_slot_para_datetime(segunda_opcao_id)

        resposta = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-reagendar-numero", numero_cliente, "2"),
        )

    assert resposta.status_code == 200
    db = main.SessionLocal()
    try:
        atualizado = db.query(models.Agendamento).filter_by(id=agendamento.id).first()
    finally:
        db.close()

    assert atualizado.status == "agendado"
    assert atualizado.data_hora == segundo_horario