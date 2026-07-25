from fastapi import FastAPI, Request
import httpx
from database import SessionLocal
from models import Empresa
from conversa import processar_mensagem

app = FastAPI()

EVOLUTION_URL = "http://localhost:8080"
EVOLUTION_API_KEY = "62257eae0c33e45e97912a5584c070ac"


@app.get("/")
async def raiz():
    return {"status": "meu bot está de pé"}


@app.post("/webhook")
async def receber_mensagem(request: Request):
    payload = await request.json()

    try:
        nome_instancia = payload["instance"]
        dados = payload["data"]
        de_mim_mesmo = dados["key"]["fromMe"]
        numero = dados["key"]["remoteJid"]
        texto = dados["message"]["conversation"]
    except (KeyError, TypeError):
        return {"status": "ignorado"}

    if de_mim_mesmo:
        return {"status": "ignorado_from_me"}

    db = SessionLocal()
    try:
        empresa = db.query(Empresa).filter_by(
            evolution_instance_name=nome_instancia, ativo=True
        ).first()

        if not empresa:
            return {"status": "empresa_nao_encontrada"}

        resposta = processar_mensagem(db, empresa, numero, texto)

        if resposta:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{EVOLUTION_URL}/message/sendText/{nome_instancia}",
                    headers={"apikey": EVOLUTION_API_KEY},
                    json={"number": numero, "text": resposta}
                )
    finally:
        db.close()

    return {"status": "ok"}