from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(validation_alias="DATABASE_URL")
    redis_url: str = Field(validation_alias="REDIS_URL")
    evolution_url: str = Field(default="http://localhost:8080", validation_alias="EVOLUTION_URL")
    evolution_api_key: str = Field(validation_alias="EVOLUTION_API_KEY")
    evolution_instance: str = Field(default="teste-aprendizado", validation_alias="EVOLUTION_INSTANCE")
    meta_token: str = Field(validation_alias="META_TOKEN")
    meta_phone_number_id: str = Field(validation_alias="META_PHONE_NUMBER_ID")
    meta_business_id: str | None = Field(default=None, validation_alias="META_BUSINESS_ID")
    bot_activation_words_raw: str = Field(default="oibot", validation_alias="BOT_ACTIVATION_WORDS")
    seed_empresa_slug: str = Field(default="sorriso-feliz", validation_alias="SEED_EMPRESA_SLUG")
    seed_empresa_nome: str = Field(default="Clínica Sorriso Feliz", validation_alias="SEED_EMPRESA_NOME")
    seed_empresa_segmento: str = Field(default="clinica", validation_alias="SEED_EMPRESA_SEGMENTO")
    seed_empresa_telefone_whatsapp: str = Field(
        default="5586999999999",
        validation_alias="SEED_EMPRESA_TELEFONE_WHATSAPP",
    )
    seed_empresa_evolution_instance_name: str = Field(
        default="teste-aprendizado",
        validation_alias="SEED_EMPRESA_EVOLUTION_INSTANCE",
    )
    admin_username: str = Field(default="admin", validation_alias="ADMIN_USERNAME")
    admin_password: str = Field(validation_alias="ADMIN_PASSWORD")
    session_secret_key: str = Field(validation_alias="SESSION_SECRET_KEY")
    meta_template_lembrete_nome: str = Field(
        default="lembrete_agendamento", validation_alias="META_TEMPLATE_LEMBRETE_NOME"
    )
    meta_template_lembrete_idioma: str = Field(
        default="pt_BR", validation_alias="META_TEMPLATE_LEMBRETE_IDIOMA"
    )
    lembrete_antecedencia_horas: int = Field(default=24, validation_alias="LEMBRETE_ANTECEDENCIA_HORAS")
    lembrete_intervalo_minutos: int = Field(default=15, validation_alias="LEMBRETE_INTERVALO_MINUTOS")
    ai_enabled: bool = Field(default=False, validation_alias="AI_ENABLED")
    ai_provider: str = Field(default="openai", validation_alias="AI_PROVIDER")
    ai_api_key: str | None = Field(default=None, validation_alias="AI_API_KEY")
    ai_model: str = Field(default="gpt-4o-mini", validation_alias="AI_MODEL")
    ai_timeout_segundos: float = Field(default=6.0, validation_alias="AI_TIMEOUT_SEGUNDOS")
    ai_cache_ttl_segundos: int = Field(default=600, validation_alias="AI_CACHE_TTL_SEGUNDOS")

    @field_validator("admin_password")
    @classmethod
    def validar_admin_password(cls, value: str) -> str:
        if not value or value == "admin123":
            raise ValueError("ADMIN_PASSWORD deve ser definido com um valor forte")
        return value

    @field_validator("session_secret_key")
    @classmethod
    def validar_session_secret_key(cls, value: str) -> str:
        if not value or value == "change-me-in-production" or len(value) < 16:
            raise ValueError("SESSION_SECRET_KEY deve ser definido com um valor forte de pelo menos 16 caracteres")
        return value

    @property
    def bot_activation_words(self) -> tuple[str, ...]:
        palavras = [
            palavra.strip().lower()
            for palavra in self.bot_activation_words_raw.split(",")
            if palavra.strip()
        ]
        return tuple(palavras) if palavras else ("oibot",)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()