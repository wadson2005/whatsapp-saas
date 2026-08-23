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
