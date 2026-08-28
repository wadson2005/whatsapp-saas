import importlib
from pathlib import Path

import httpx
import pytest

from conftest import preparar_ambiente


def carregar_modulo(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    return importlib.import_module("integrations.evolution_client")


class _RespostaFalsa:
    def __init__(self, status_code: int, corpo: dict | None):
        self.status_code = status_code
        self._corpo = corpo
        self.text = "" if corpo is None else str(corpo)

    def json(self):
        if self._corpo is None:
            raise ValueError("sem corpo")
        return self._corpo


class _ClienteFalso:
    def __init__(self, resposta=None, excecao=None):
        self._resposta = resposta
        self._excecao = excecao
        self.chamadas = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        self.chamadas.append((method, url, kwargs))
        if self._excecao:
            raise self._excecao
        return self._resposta


def _instalar_cliente_falso(monkeypatch, modulo, cliente_falso):
    monkeypatch.setattr(modulo.httpx, "AsyncClient", lambda *a, **k: cliente_falso)
    return cliente_falso


def test_criar_instancia_monta_payload_correto(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    cliente_falso = _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(_RespostaFalsa(201, {"instance": {"status": "created"}})),
    )

    import asyncio

    resultado = asyncio.run(
        evolution_client.criar_instancia("clinica-a", "5511999999999", "https://exemplo.com/webhook")
    )

    assert resultado == {"instance": {"status": "created"}}
    method, url, kwargs = cliente_falso.chamadas[0]
    assert method == "POST"
    assert url.endswith("/instance/create")
    assert kwargs["json"]["instanceName"] == "clinica-a"
    assert kwargs["json"]["number"] == "5511999999999"
    assert kwargs["json"]["webhook"]["url"] == "https://exemplo.com/webhook"
    assert kwargs["json"]["integration"] == "WHATSAPP-BAILEYS"


def test_estado_conexao_extrai_state(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(_RespostaFalsa(200, {"instance": {"instanceName": "clinica-a", "state": "open"}})),
    )

    import asyncio

    estado = asyncio.run(evolution_client.estado_conexao("clinica-a"))
    assert estado == "open"


def test_requisicao_com_status_de_erro_levanta_evolution_api_error(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(_RespostaFalsa(404, {"message": "Instance not found"})),
    )

    import asyncio

    with pytest.raises(evolution_client.EvolutionAPIError, match="Instance not found"):
        asyncio.run(evolution_client.estado_conexao("nao-existe"))


def test_falha_de_rede_levanta_evolution_api_conexao_error(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(excecao=httpx.ConnectError("connection refused")),
    )

    import asyncio

    with pytest.raises(evolution_client.EvolutionAPIConexaoError):
        asyncio.run(evolution_client.gerar_qrcode("clinica-a", "5511999999999"))


def test_excluir_instancia_nunca_levanta_mesmo_com_falha(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(_RespostaFalsa(500, {"message": "erro interno"})),
    )

    import asyncio

    asyncio.run(evolution_client.excluir_instancia("clinica-a"))  # não deve levantar


def test_qrcode_para_json_escapa_fechamento_de_script(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)

    resultado = evolution_client.qrcode_para_json({"code": "2@abc</script><script>alert(1)"})
    assert "</script>" not in resultado
    assert "<\\/script>" in resultado


def test_qrcode_para_json_com_none_vira_null(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)

    assert evolution_client.qrcode_para_json(None) == "null"


# --- enviar_botoes / enviar_texto: cada empresa responde pela própria instância ----


def test_enviar_botoes_usa_a_instancia_informada_na_url(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    cliente_falso = _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(_RespostaFalsa(201, {"key": {"id": "abc"}})),
    )

    import asyncio

    asyncio.run(
        evolution_client.enviar_botoes(
            instance="clinica-a",
            numero="5511900000001",
            texto="Escolha uma opção",
            botoes=[{"id": "menu", "titulo": "Menu"}, {"id": "sair", "titulo": "Sair"}],
            rodape="Rodapé",
        )
    )

    method, url, kwargs = cliente_falso.chamadas[0]
    assert method == "POST"
    assert url.endswith("/message/sendButtons/clinica-a")
    payload = kwargs["json"]
    assert payload["number"] == "5511900000001"
    assert payload["description"] == "Escolha uma opção"
    assert payload["footer"] == "Rodapé"
    assert payload["buttons"] == [
        {"type": "reply", "displayText": "Menu", "id": "menu"},
        {"type": "reply", "displayText": "Sair", "id": "sair"},
    ]


def test_enviar_botoes_limita_a_3_botoes(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    cliente_falso = _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(_RespostaFalsa(201, {})),
    )

    import asyncio

    botoes = [{"id": f"b{i}", "titulo": f"Botão {i}"} for i in range(5)]
    asyncio.run(evolution_client.enviar_botoes(instance="clinica-a", numero="5511900000001", texto="x", botoes=botoes))

    _, _, kwargs = cliente_falso.chamadas[0]
    assert len(kwargs["json"]["buttons"]) == 3


def test_enviar_botoes_sem_instancia_recusa_o_envio(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    cliente_falso = _instalar_cliente_falso(monkeypatch, evolution_client, _ClienteFalso())

    import asyncio

    with pytest.raises(evolution_client.InstanciaNaoConfiguradaError):
        asyncio.run(evolution_client.enviar_botoes(instance=None, numero="5511900000001", texto="x", botoes=[]))
    with pytest.raises(evolution_client.InstanciaNaoConfiguradaError):
        asyncio.run(evolution_client.enviar_botoes(instance="", numero="5511900000001", texto="x", botoes=[]))

    assert cliente_falso.chamadas == []  # nunca chega a chamar a Evolution API


def test_enviar_texto_usa_a_instancia_informada_na_url(monkeypatch, tmp_path):
    """enviar_texto substitui o antigo enviar_lista (sendList), que quebra na
    Evolution API v2.3.6/Baileys com um erro interno ('this.isZero is not a
    function'), confirmado direto contra a API real em produção."""
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    cliente_falso = _instalar_cliente_falso(
        monkeypatch,
        evolution_client,
        _ClienteFalso(_RespostaFalsa(201, {})),
    )

    import asyncio

    asyncio.run(
        evolution_client.enviar_texto(
            instance="clinica-b",
            numero="5511900000002",
            texto="Escolha um serviço\n\n• Corte — R$ 50",
        )
    )

    method, url, kwargs = cliente_falso.chamadas[0]
    assert method == "POST"
    assert url.endswith("/message/sendText/clinica-b")
    payload = kwargs["json"]
    assert payload["number"] == "5511900000002"
    assert payload["text"] == "Escolha um serviço\n\n• Corte — R$ 50"


def test_enviar_texto_sem_instancia_recusa_o_envio(monkeypatch, tmp_path):
    evolution_client = carregar_modulo(monkeypatch, tmp_path)
    cliente_falso = _instalar_cliente_falso(monkeypatch, evolution_client, _ClienteFalso())

    import asyncio

    with pytest.raises(evolution_client.InstanciaNaoConfiguradaError):
        asyncio.run(evolution_client.enviar_texto(instance=None, numero="5511900000001", texto="x"))

    assert cliente_falso.chamadas == []
