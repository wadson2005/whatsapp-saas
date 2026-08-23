import asyncio
import logging
import smtplib
from email.message import EmailMessage

from core.config import settings

logger = logging.getLogger(__name__)


class EmailError(Exception):
    """Levantada quando o e-mail não está configurado ou o envio falha."""


def email_configurado() -> bool:
    return bool(settings.smtp_host and settings.smtp_usuario and settings.smtp_senha and settings.smtp_remetente)


def _enviar_sync(destinatario: str, assunto: str, corpo_texto: str) -> None:
    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = settings.smtp_remetente
    mensagem["To"] = destinatario
    mensagem.set_content(corpo_texto)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_porta, timeout=10) as servidor:
        if settings.smtp_use_tls:
            servidor.starttls()
        servidor.login(settings.smtp_usuario, settings.smtp_senha)
        servidor.send_message(mensagem)


async def enviar_email(destinatario: str, assunto: str, corpo_texto: str) -> None:
    """Envia um e-mail via SMTP (rodando o cliente síncrono numa thread à parte).

    Levanta `EmailError` se o SMTP não estiver configurado ou se o envio falhar
    (rede, autenticação, recusa do servidor) — nunca deixa a exceção original do
    `smtplib` vazar para o chamador.
    """
    if not email_configurado():
        raise EmailError("Envio de e-mail não está configurado (SMTP_HOST/SMTP_USUARIO/SMTP_SENHA/SMTP_REMETENTE).")

    try:
        await asyncio.to_thread(_enviar_sync, destinatario, assunto, corpo_texto)
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Falha ao enviar e-mail para %s: %s", destinatario, exc)
        raise EmailError("Não foi possível enviar o e-mail.") from exc
