import os
import httpx
from dotenv import load_dotenv

load_dotenv()

META_TOKEN = os.getenv("META_TOKEN")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
META_API_URL = f"https://graph.facebook.com/v21.0/{META_PHONE_NUMBER_ID}/messages"


async def enviar_botoes(numero: str, texto: str, botoes: list[dict], rodape: str | None = None):
    """
    botoes: lista de dicts no formato {"id": "...", "titulo": "..."}
    Máximo de 3 botões (limite da própria Meta).
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

    async with httpx.AsyncClient() as client:
        resposta = await client.post(
            META_API_URL,
            headers={"Authorization": f"Bearer {META_TOKEN}"},
            json=payload,
        )
        resultado = resposta.json()
        print(f"[DEBUG META] enviar_botoes -> status {resposta.status_code}: {resultado}")
        return resultado


async def enviar_lista(numero: str, texto: str, titulo_botao: str, secoes: list[dict], rodape: str | None = None):
    """
    Para quando há mais de 3 opções (ex: lista de serviços) — usa o componente de
    lista da Meta em vez de botões, que tem limite de 3.
    secoes: [{"titulo": "Serviços", "linhas": [{"id": "...", "titulo": "...", "descricao": "..."}]}]
    """
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
                    for s in secoes
                ],
            },
        },
    }
    if rodape:
        payload["interactive"]["footer"] = {"text": rodape}

    async with httpx.AsyncClient() as client:
        resposta = await client.post(
            META_API_URL,
            headers={"Authorization": f"Bearer {META_TOKEN}"},
            json=payload,
        )
        resultado = resposta.json()
        print(f"[DEBUG META] enviar_botoes -> status {resposta.status_code}: {resultado}")
        return resultado