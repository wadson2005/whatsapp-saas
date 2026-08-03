import importlib
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_ENV = {
    "DATABASE_URL": "postgresql://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/1",
    "EVOLUTION_API_KEY": "x",
    "META_TOKEN": "x",
    "META_PHONE_NUMBER_ID": "x",
    "ADMIN_PASSWORD": "senha-super-segura-123",
    "SESSION_SECRET_KEY": "0123456789abcdef0123456789abcdef",
}


def carregar_settings():
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    for chave, valor in BOOTSTRAP_ENV.items():
        os.environ.setdefault(chave, valor)

    sys.modules.pop("core.config", None)
    return importlib.import_module("core.config").Settings


@pytest.mark.parametrize("env_value", [None, ""])
def test_settings_rejeita_segredos_fracos_ou_ausentes(monkeypatch, env_value):
    Settings = carregar_settings()

    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    if env_value is not None:
        monkeypatch.setenv("ADMIN_PASSWORD", env_value)
        monkeypatch.setenv("SESSION_SECRET_KEY", env_value)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_rejeita_placeholders_e_secret_curto(monkeypatch):
    Settings = carregar_settings()

    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
    monkeypatch.setenv("SESSION_SECRET_KEY", "change-me-in-production")

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    mensagem = str(exc_info.value)
    assert "ADMIN_PASSWORD" in mensagem
    assert "SESSION_SECRET_KEY" in mensagem


def test_settings_aceita_valores_fortes(monkeypatch):
    Settings = carregar_settings()

    monkeypatch.setenv("ADMIN_PASSWORD", "senha-super-segura-123")
    monkeypatch.setenv("SESSION_SECRET_KEY", "0123456789abcdef0123456789abcdef")

    settings = Settings()

    assert settings.admin_password == "senha-super-segura-123"
    assert settings.session_secret_key == "0123456789abcdef0123456789abcdef"
