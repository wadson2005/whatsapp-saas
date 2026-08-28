import json
import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SEGUNDOS = 15.0


class EvolutionAPIError(Exception):
    """Levantada quando a Evolution API recusa a chamada (HTTP >= 400)."""


class EvolutionAPIConexaoError(EvolutionAPIError):
    """Levantada quando não foi possível alcançar a Evolution API (rede, timeout, DNS)."""


class InstanciaNaoConfiguradaError(Exception):
    """Levantada quando a empresa não tem uma instância Evolution válida para enviar mensagens.

    Nunca deve ser contornada com um número/instância global — cada empresa só
    pode responder pelo próprio número, conectado via QR code. Sem instância,
    a falha é explícita (exceção + log), não um envio silencioso por outro canal.
    """


def _headers() -> dict[str, str]:
    return {"apikey": settings.evolution_api_key, "Content-Type": "application/json"}


async def _requisitar(method: str, caminho: str, **kwargs) -> dict:
    url = f"{settings.evolution_url}{caminho}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as client:
            resposta = await client.request(method, url, headers=_headers(), **kwargs)
    except httpx.HTTPError as exc:
        logger.error("Falha de rede ao chamar a Evolution API (%s %s): %s", method, caminho, exc)
        raise EvolutionAPIConexaoError("Não foi possível conectar à Evolution API.") from exc

    try:
        corpo = resposta.json()
    except ValueError:
        corpo = {}

    if resposta.status_code >= 400:
        mensagem = corpo.get("message") or corpo.get("error") or resposta.text or f"HTTP {resposta.status_code}"
        logger.error("Evolution API respondeu %s em %s %s: %s", resposta.status_code, method, caminho, mensagem)
        raise EvolutionAPIError(str(mensagem))

    return corpo


async def criar_instancia(nome_instancia: str, numero: str, webhook_url: str) -> dict:
    """Cria a instância na Evolution API e já configura o webhook apontando para este bot.

    O QR code de pareamento não vem nessa resposta — chame `gerar_qrcode()` em
    seguida para obtê-lo.
    """
    payload = {
        "instanceName": nome_instancia,
        "number": numero,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
        "webhook": {
            "url": webhook_url,
            "byEvents": False,
            "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE"],
        },
    }
    return await _requisitar("POST", "/instance/create", json=payload)


async def gerar_qrcode(nome_instancia: str, numero: str) -> dict:
    """Gera (ou renova) o código de pareamento de uma instância já criada.

    Retorna o dict cru da Evolution API — campos usados hoje: `pairingCode`
    (código curto para digitar em "Conectar com número de telefone" no
    WhatsApp) e `code` (dado bruto do QR).
    """
    return await _requisitar("GET", f"/instance/connect/{nome_instancia}", params={"number": numero})


async def estado_conexao(nome_instancia: str) -> str:
    """Retorna o estado da conexão: 'open' (conectado), 'connecting' ou 'close'."""
    resultado = await _requisitar("GET", f"/instance/connectionState/{nome_instancia}")
    return resultado.get("instance", {}).get("state", "close")


async def instancia_existe(nome_instancia: str) -> bool:
    """Verifica se já existe uma instância com esse nome na Evolution API.

    Usado antes de `criar_instancia` para dar uma mensagem clara quando o slug
    escolhido já tem uma instância associada (a própria Evolution API responde
    só "Forbidden" nesse caso, sem dizer o motivo). Se a Evolution API estiver
    inacessível, retorna `False` — `criar_instancia` vai falhar logo em seguida
    com o erro de conectividade real.
    """
    try:
        await estado_conexao(nome_instancia)
    except EvolutionAPIError:
        return False
    return True


def qrcode_para_json(qrcode: dict | None) -> str:
    """Serializa o resultado de `gerar_qrcode` para embutir num <script type="application/json">.

    Escapa "</" para o dado não conseguir fechar a tag <script> que o envolve.
    """
    return json.dumps(qrcode).replace("</", "<\\/")


async def excluir_instancia(nome_instancia: str) -> None:
    """Remove uma instância da Evolution API. Usado para desfazer uma criação parcial.

    Nunca levanta exceção — é sempre uma limpeza best-effort chamada a partir
    de um bloco que já está tratando outro erro.
    """
    try:
        await _requisitar("DELETE", f"/instance/delete/{nome_instancia}")
    except EvolutionAPIError as exc:
        logger.warning("Falha ao excluir instância '%s' na Evolution API: %s", nome_instancia, exc)


def _validar_instancia(instance: str | None) -> str:
    """Garante que existe uma instância explícita antes de qualquer envio.

    Ponto único onde essa checagem acontece — nenhuma função de envio deste
    módulo tem valor padrão para `instance`, então chamar sem passar a
    instância da própria empresa já é um erro de programação (`TypeError`)
    antes mesmo de chegar aqui.
    """
    if not instance:
        logger.error("Tentativa de enviar mensagem sem instância Evolution associada à empresa — envio recusado.")
        raise InstanciaNaoConfiguradaError(
            "Empresa sem instância Evolution configurada — não há número dela para enviar a resposta."
        )
    return instance


async def enviar_botoes(instance: str, numero: str, texto: str, botoes: list[dict], rodape: str | None = None) -> dict:
    """Envia até 3 botões de resposta rápida pela instância (número) da própria empresa.

    `instance` é sempre obrigatório e explícito — nunca há fallback para outra
    instância/número. botoes: lista de dicts no formato {"id": "...", "titulo": "..."}.
    """
    _validar_instancia(instance)
    payload = {
        "number": numero,
        "title": " ",  # WhatsApp exige um título; o conteúdo de verdade vai em "description"
        "description": texto,
        "footer": rodape or "",
        "buttons": [
            {"type": "reply", "displayText": b["titulo"], "id": b["id"]}
            for b in botoes[:3]
        ],
    }
    return await _requisitar("POST", f"/message/sendButtons/{instance}", json=payload)


async def enviar_texto(instance: str, numero: str, texto: str) -> dict:
    """Envia uma mensagem de texto simples pela instância (número) da própria empresa.

    `instance` é sempre obrigatório e explícito — nunca há fallback para outra
    instância/número. Usada para menus de opções: a Evolution API v2.3.6 tem
    um erro interno em `/message/sendList` (`TypeError: this.isZero is not a
    function`, confirmado em produção contra a API real, não é um problema do
    nosso payload) — os fluxos que mostram opções escrevem a lista dentro do
    próprio texto e o cliente responde digitando a escolha.
    """
    _validar_instancia(instance)
    payload = {"number": numero, "text": texto}
    return await _requisitar("POST", f"/message/sendText/{instance}", json=payload)
