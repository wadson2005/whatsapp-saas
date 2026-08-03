import httpx

from configuracoes import obter_configuracao_isolada


def _api_url_e_headers() -> tuple[str, dict[str, str]]:
    config = obter_configuracao_isolada()
    url = f"https://graph.facebook.com/v21.0/{config.meta_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {config.meta_token}"}
    return url, headers


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

    url, headers = _api_url_e_headers()
    async with httpx.AsyncClient() as client:
        resposta = await client.post(url, headers=headers, json=payload)
        resultado = resposta.json()
        print(f"[DEBUG META] enviar_botoes -> status {resposta.status_code}: {resultado}")
        return resultado


async def enviar_lista(numero: str, texto: str, titulo_botao: str, secoes: list[dict], rodape: str | None = None):
    """
    Para quando há mais de 3 opções (ex: lista de serviços) — usa o componente de
    lista da Meta em vez de botões, que tem limite de 3.
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

    url, headers = _api_url_e_headers()
    async with httpx.AsyncClient() as client:
        resposta = await client.post(url, headers=headers, json=payload)
        resultado = resposta.json()
        print(f"[DEBUG META] enviar_botoes -> status {resposta.status_code}: {resultado}")
        return resultado


async def enviar_template(numero: str, nome_template: str, idioma: str, parametros_corpo: list[str]):
    """
    Envia mensagem via template pré-aprovado da Meta (obrigatório fora da janela de
    24h de atendimento ao cliente, quando não é permitido enviar texto/interativo livre).
    parametros_corpo: valores posicionais para os placeholders {{1}}, {{2}}... do corpo do template.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "template",
        "template": {
            "name": nome_template,
            "language": {"code": idioma},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": valor} for valor in parametros_corpo],
                }
            ],
        },
    }

    url, headers = _api_url_e_headers()
    async with httpx.AsyncClient() as client:
        resposta = await client.post(url, headers=headers, json=payload)
        resultado = resposta.json()
        print(f"[DEBUG META] enviar_template -> status {resposta.status_code}: {resultado}")
        return resultado
