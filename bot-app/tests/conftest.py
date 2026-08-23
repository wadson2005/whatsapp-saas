import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BOOTSTRAP_ENV = {
    "REDIS_URL": "redis://localhost:6379/1",
    "EVOLUTION_API_KEY": "x",
    "META_TOKEN": "x",
    "META_PHONE_NUMBER_ID": "x",
    "PUBLIC_BASE_URL": "https://teste.exemplo.com",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "senha-super-segura-123",
    "SESSION_SECRET_KEY": "0123456789abcdef0123456789abcdef",
    "WEBHOOK_SECRET": "0123456789abcdef0123456789abcdef",
}

ADMIN_PASSWORD = BOOTSTRAP_ENV["ADMIN_PASSWORD"]
WEBHOOK_SECRET = BOOTSTRAP_ENV["WEBHOOK_SECRET"]

# Módulos da aplicação que precisam ser recarregados a cada teste: cada um lê
# `config.settings` (ou algo que depende dela) na importação, então o cache do
# Python em sys.modules mascararia as variáveis de ambiente definidas pelo teste
# anterior se não for limpo antes de cada `importlib.import_module`.
APP_MODULES = [
    "main",
    "admin",
    "conversa",
    "core",
    "core.config",
    "core.database",
    "core.db_compat",
    "core.models",
    "core.redis_client",
    "core.schema",
    "core.security",
    "services",
    "services.agenda",
    "services.atendimento_humano",
    "services.configuracoes",
    "services.conhecimento",
    "services.lembretes",
    "services.metricas",
    "services.texto_utils",
    "services.usuarios",
    "integrations",
    "integrations.evolution_client",
    "integrations.meta_client",
    "ai",
    "ai.provider",
    "ai.service",
    "ai.prompts",
    "ai.models",
    "ai.cache",
]


class FakeRedis:
    """Substituto em memória para o cliente Redis nos testes que não sobem um Redis real."""

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


def preparar_ambiente(monkeypatch, tmp_path: Path, env_overrides: dict | None = None):
    """Aponta a aplicação para um SQLite temporário e força reimport com env isolado."""
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    database_path = tmp_path / "bot-app.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    for chave, valor in {**BOOTSTRAP_ENV, **(env_overrides or {})}.items():
        monkeypatch.setenv(chave, valor)

    for modulo in APP_MODULES:
        sys.modules.pop(modulo, None)
