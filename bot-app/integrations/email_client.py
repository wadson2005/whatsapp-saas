from __future__ import annotations

import logging

import httpx

from services.configuracoes import obter_configuracao_isolada

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
TIMEOUT_SEGUNDOS = 15.0


class EmailError(Exception):
    """Levantada quando o e-mail não está configurado ou o envio falha."""


def email_configurado(config=None) -> bool:
    config = config or obter_configuracao_isolada()
    return bool(config.resend_api_key and config.email_from_endereco)


async def enviar_email(destinatario: str, assunto: str, corpo_texto: str) -> None:
    """Envia um e-mail via API do Resend.

    Levanta `EmailError` se o Resend não estiver configurado ou se o envio
    falhar (rede, autenticação, recusa da API) — nunca deixa a exceção
    original do httpx vazar para o chamador.
    """
    config = obter_configuracao_isolada()
    if not email_configurado(config):
        raise EmailError("Envio de e-mail não está configurado (RESEND_API_KEY/EMAIL_FROM_ENDERECO).")

    remetente = config.email_from_endereco
    if config.email_from_nome:
        remetente = f"{config.email_from_nome} <{config.email_from_endereco}>"

    payload = {
        "from": remetente,
        "to": [destinatario],
        "subject": assunto,
        "text": corpo_texto,
    }
    headers = {"Authorization": f"Bearer {config.resend_api_key}"}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as client:
            resposta = await client.post(RESEND_API_URL, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.error("Falha de rede ao chamar a API do Resend: %s", exc)
        raise EmailError("Não foi possível enviar o e-mail.") from exc

    if resposta.status_code >= 400:
        try:
            detalhe = resposta.json().get("message", resposta.text)
        except ValueError:
            detalhe = resposta.text
        logger.error("Resend recusou o envio para %s: %s", destinatario, detalhe)
        raise EmailError(f"Resend recusou o envio: {detalhe}")
