from fastapi import FastAPI, HTTPException, Request
import httpx
from sqlalchemy import text
from admin import admin_app
from config import settings
from database import SessionLocal
from models import Empresa
from redis_client import redis_cliente
from conversa import processar_mensagem
from schema import ensure_schema

app = FastAPI()
app.mount("/admin", admin_app)

EVOLUTION_URL = settings.evolution_url
EVOLUTION_API_KEY = settings.evolution_api_key


@app.get("/")
async def raiz():
    return {"status": "meu bot está de pé"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        redis_cliente.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="servico_indisponivel") from exc
    finally:
        db.close()

    return {"status": "ok", "database": "ok", "redis": "ok"}


@app.on_event("startup")
async def startup_schema():
    ensure_schema()


def extrair_conteudo(dados: dict) -> tuple[str | None, str | None]:
    """
    Retorna (texto, id_interacao).
    - Se for texto digitado: (texto_digitado, None)
    - Se for clique em botão/lista: (titulo_clicado, id_do_botao)
    """
    mensagem = dados.get("message", {})

    if "conversation" in mensagem:
        return mensagem["conversation"], None

    if "interactive" in mensagem:
        interativo = mensagem["interactive"]
        if interativo.get("type") == "button_reply":
            br = interativo["button_reply"]
            return br["title"], br["id"]
        if interativo.get("type") == "list_reply":
            lr = interativo["list_reply"]
            return lr["title"], lr["id"]

    return None, None


@app.post("/webhook")
async def receber_mensagem(request: Request):
    payload = await request.json()
    print(f"[DEBUG PAYLOAD COMPLETO] {payload}")
    try:
        nome_instancia = payload["instance"]
        dados = payload["data"]
        de_mim_mesmo = dados["key"]["fromMe"]
        numero = dados["key"]["remoteJid"]
    except (KeyError, TypeError):
        return {"status": "ignorado"}

    if de_mim_mesmo:
        return {"status": "ignorado_from_me"}

    texto, id_interacao = extrair_conteudo(dados)
    if texto is None:
        return {"status": "tipo_de_mensagem_nao_suportado"}

    db = SessionLocal()
    try:
        empresa = db.query(Empresa).filter_by(
            evolution_instance_name=nome_instancia, ativo=True
        ).first()

        if not empresa:
            return {"status": "empresa_nao_encontrada"}

        # id_interacao tem prioridade sobre o texto quando existe (clique é mais preciso que texto)
        await processar_mensagem(db, empresa, numero, texto, id_interacao)
    finally:
        db.close()

    return {"status": "ok"}