import asyncio
import importlib
from pathlib import Path

import httpx
import pytest

from conftest import preparar_ambiente


def carregar_modulo(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    email_client = importlib.import_module("integrations.email_client")
    configuracoes = importlib.import_module("services.configuracoes")
    main.ensure_schema()
    return email_client, configuracoes, main


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

    async def post(self, url, headers=None, json=None):
        self.chamadas.append((url, headers, json))
        if self._excecao:
            raise self._excecao
        return self._resposta


def _configurar_resend(configuracoes, main, api_key="re_teste123", remetente="lembretes@exemplo.com", nome="Minha Empresa"):
    db = main.SessionLocal()
    try:
        configuracoes.atualizar_configuracao(
            db, resend_api_key=api_key, email_from_endereco=remetente, email_from_nome=nome
        )
    finally:
        db.close()


def test_email_configurado_false_sem_credenciais(monkeypatch, tmp_path):
    email_client, _, _ = carregar_modulo(monkeypatch, tmp_path)
    assert email_client.email_configurado() is False


def test_enviar_email_sem_configuracao_levanta_email_error(monkeypatch, tmp_path):
    email_client, _, _ = carregar_modulo(monkeypatch, tmp_path)

    with pytest.raises(email_client.EmailError):
        asyncio.run(email_client.enviar_email("cliente@exemplo.com", "Assunto", "Corpo"))


def test_enviar_email_monta_payload_e_headers_corretos(monkeypatch, tmp_path):
    email_client, configuracoes, main = carregar_modulo(monkeypatch, tmp_path)
    _configurar_resend(configuracoes, main)

    cliente_falso = _ClienteFalso(_RespostaFalsa(200, {"id": "email-123"}))
    monkeypatch.setattr(email_client.httpx, "AsyncClient", lambda *a, **k: cliente_falso)

    asyncio.run(email_client.enviar_email("cliente@exemplo.com", "Lembrete", "Seu horário é amanhã."))

    assert len(cliente_falso.chamadas) == 1
    url, headers, payload = cliente_falso.chamadas[0]
    assert url == email_client.RESEND_API_URL
    assert headers["Authorization"] == "Bearer re_teste123"
    assert payload["from"] == "Minha Empresa <lembretes@exemplo.com>"
    assert payload["to"] == ["cliente@exemplo.com"]
    assert payload["subject"] == "Lembrete"
    assert payload["text"] == "Seu horário é amanhã."


def test_enviar_email_sem_nome_remetente_usa_so_endereco(monkeypatch, tmp_path):
    email_client, configuracoes, main = carregar_modulo(monkeypatch, tmp_path)
    _configurar_resend(configuracoes, main, nome=None)

    cliente_falso = _ClienteFalso(_RespostaFalsa(200, {"id": "email-123"}))
    monkeypatch.setattr(email_client.httpx, "AsyncClient", lambda *a, **k: cliente_falso)

    asyncio.run(email_client.enviar_email("cliente@exemplo.com", "Lembrete", "Corpo"))

    _, _, payload = cliente_falso.chamadas[0]
    assert payload["from"] == "lembretes@exemplo.com"


def test_enviar_email_status_erro_levanta_email_error(monkeypatch, tmp_path):
    email_client, configuracoes, main = carregar_modulo(monkeypatch, tmp_path)
    _configurar_resend(configuracoes, main)

    cliente_falso = _ClienteFalso(_RespostaFalsa(422, {"name": "validation_error", "message": "endereço inválido"}))
    monkeypatch.setattr(email_client.httpx, "AsyncClient", lambda *a, **k: cliente_falso)

    with pytest.raises(email_client.EmailError, match="endereço inválido"):
        asyncio.run(email_client.enviar_email("invalido", "Assunto", "Corpo"))


def test_enviar_email_falha_de_rede_levanta_email_error(monkeypatch, tmp_path):
    email_client, configuracoes, main = carregar_modulo(monkeypatch, tmp_path)
    _configurar_resend(configuracoes, main)

    cliente_falso = _ClienteFalso(excecao=httpx.ConnectTimeout("timeout"))
    monkeypatch.setattr(email_client.httpx, "AsyncClient", lambda *a, **k: cliente_falso)

    with pytest.raises(email_client.EmailError):
        asyncio.run(email_client.enviar_email("cliente@exemplo.com", "Assunto", "Corpo"))
