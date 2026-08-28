import logging

import httpx

from services.configuracoes import obter_configuracao_isolada

logger = logging.getLogger(__name__)


def _api_url_e_headers() -> tuple[str, dict[str, str]]:
    config = obter_configuracao_isolada()
    url = f"https://graph.facebook.com/v21.0/{config.meta_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {config.meta_token}"}
    return url, headers


async def _enviar(payload: dict) -> dict:
    """POSTa na Graph API e devolve o JSON de resposta.

    Nunca deixa um erro de rede (timeout, DNS, conexão recusada) propagar como
    exceção — devolve `{"error": {...}}` para que o chamador trate do mesmo jeito
    que trataria uma rejeição da própria Meta (ver `lembretes.enviar_lembrete`).
    """
    url, headers = _api_url_e_headers()
    try:
        async with httpx.AsyncClient() as client:
            resposta = await client.post(url, headers=headers, json=payload)
            resultado = resposta.json()
    except httpx.HTTPError as exc:
        logger.error("Falha de rede ao chamar a Graph API: %s", exc)
        return {"error": {"message": str(exc)}}

    logger.debug("Graph API respondeu status=%s: %s", resposta.status_code, resultado)
    return resultado


async def enviar_botoes(numero: str, texto: str, botoes: list[dict], rodape: str | None = None) -> dict:
    """Envia até 3 botões de resposta rápida (limite da própria Meta).

    botoes: lista de dicts no formato {"id": "...", "titulo": "..."}
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["titulo"]}}
                    for b in botoes[:3]
                ]
            },
        },
    }
    if rodape:
        payload["interactive"]["footer"] = {"text": rodape}

    return await _enviar(payload)


async def enviar_lista(numero: str, texto: str, titulo_botao: str, secoes: list[dict], rodape: str | None = None) -> dict:
    """Envia uma lista de opções (para quando há mais de 3, limite dos botões simples).

    secoes: [{"titulo": "Serviços", "linhas": [{"id": "...", "titulo": "...", "descricao": "..."}]}]
    """
    MAX_LINHAS_TOTAL = 10
    secoes_limitadas = []
    linhas_restantes = MAX_LINHAS_TOTAL

    for secao in secoes:
        if linhas_restantes <= 0:
            break

        linhas = secao.get("linhas", [])[:linhas_restantes]
        if not linhas:
            continue

        secoes_limitadas.append({"titulo": secao["titulo"], "linhas": linhas})
        linhas_restantes -= len(linhas)

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {
                "button": titulo_botao,
                "sections": [
                    {
                        "title": s["titulo"],
                        "rows": [
                            {"id": l["id"], "title": l["titulo"], "description": l.get("descricao", "")}
                            for l in s["linhas"]
                        ],
                    }
                    for s in secoes_limitadas
                ],
            },
        },
    }
    if rodape:
        payload["interactive"]["footer"] = {"text": rodape}

    return await _enviar(payload)
