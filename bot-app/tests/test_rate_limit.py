import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import FakeRedis, preparar_ambiente


def carregar_modulo(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    return importlib.import_module("core.rate_limit")


def test_dentro_do_limite_nao_bloqueia(monkeypatch, tmp_path):
    rate_limit = carregar_modulo(monkeypatch, tmp_path)

    for _ in range(5):
        assert rate_limit.excedeu_limite("chave-teste", limite=5, janela_segundos=60) is False


def test_excede_o_limite_bloqueia(monkeypatch, tmp_path):
    rate_limit = carregar_modulo(monkeypatch, tmp_path)

    for _ in range(5):
        rate_limit.excedeu_limite("chave-teste", limite=5, janela_segundos=60)

    assert rate_limit.excedeu_limite("chave-teste", limite=5, janela_segundos=60) is True


def test_chaves_diferentes_nao_se_afetam(monkeypatch, tmp_path):
    rate_limit = carregar_modulo(monkeypatch, tmp_path)

    for _ in range(5):
        rate_limit.excedeu_limite("chave-a", limite=5, janela_segundos=60)

    assert rate_limit.excedeu_limite("chave-a", limite=5, janela_segundos=60) is True
    assert rate_limit.excedeu_limite("chave-b", limite=5, janela_segundos=60) is False


def test_redis_indisponivel_deixa_passar(monkeypatch, tmp_path):
    rate_limit = carregar_modulo(monkeypatch, tmp_path)

    def _levanta_erro(*args, **kwargs):
        raise ConnectionError("redis indisponível")

    monkeypatch.setattr(rate_limit.redis_cliente, "incr", _levanta_erro)

    assert rate_limit.excedeu_limite("chave-teste", limite=1, janela_segundos=60) is False


def _carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    main.ensure_schema()
    return main


def test_login_bloqueia_apos_muitas_tentativas(monkeypatch, tmp_path):
    main = _carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        for _ in range(10):
            resposta = client.post("/admin/login", data={"username": "x", "password": "errada"})
            assert resposta.status_code == 401

        bloqueada = client.post("/admin/login", data={"username": "x", "password": "errada"})

    assert bloqueada.status_code == 429
    assert "Muitas tentativas" in bloqueada.text


def test_esqueci_senha_bloqueia_apos_muitas_tentativas(monkeypatch, tmp_path):
    main = _carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        for _ in range(5):
            resposta = client.post("/admin/esqueci-senha", data={"email": "ninguem@exemplo.com"})
            assert resposta.status_code == 200

        bloqueada = client.post("/admin/esqueci-senha", data={"email": "ninguem@exemplo.com"})

    assert bloqueada.status_code == 429
    assert "Muitas tentativas" in bloqueada.text


def test_onboarding_bloqueia_apos_muitas_tentativas(monkeypatch, tmp_path):
    main = _carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        for indice in range(5):
            resposta = client.post(
                "/onboarding",
                data={"nome": "Teste", "email": f"teste{indice}@exemplo.com", "senha": "senha-super-segura"},
                follow_redirects=False,
            )
            assert resposta.status_code != 429
            client.get("/admin/logout")

        bloqueada = client.post(
            "/onboarding",
            data={"nome": "Teste", "email": "outro@exemplo.com", "senha": "senha-super-segura"},
        )

    assert bloqueada.status_code == 429
    assert "Muitas tentativas" in bloqueada.text
