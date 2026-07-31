from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent


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
    admin_password: str = Field(default="admin123", validation_alias="ADMIN_PASSWORD")
    session_secret_key: str = Field(default="change-me-in-production", validation_alias="SESSION_SECRET_KEY")

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