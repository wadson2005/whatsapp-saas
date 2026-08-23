import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import WEBHOOK_SECRET, preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    main.ensure_schema()
    return main


def _payload():
    return {
        "instance": "empresa-inexistente",
        "data": {
            "key": {"fromMe": False, "remoteJid": "5586999999999"},
            "message": {"conversation": "oi"},
        },
    }


def test_webhook_sem_token_e_rejeitado(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        resposta = client.post("/webhook", json=_payload())

    assert resposta.status_code == 401


def test_webhook_com_token_invalido_e_rejeitado(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        resposta = client.post("/webhook?token=token-forjado", json=_payload())

    assert resposta.status_code == 401


def test_webhook_com_token_correto_e_processado(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        resposta = client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload())

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "empresa_nao_encontrada"}
