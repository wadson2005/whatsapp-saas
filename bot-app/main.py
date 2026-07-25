from fastapi import FastAPI, Request
import httpx
from database import SessionLocal
from models import Empresa, Servico

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

        resposta = montar_resposta(db, empresa, texto)

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


def montar_resposta(db, empresa: Empresa, texto: str) -> str | None:
    texto_lower = texto.lower()

    if "ola" in texto_lower or "olá" in texto_lower or "oi" in texto_lower:
        servicos = db.query(Servico).filter_by(empresa_id=empresa.id, ativo=True).all()

        linhas = [f"Olá! Bem-vindo(a) à {empresa.nome} 😊", "", "Nossos serviços:"]
        for s in servicos:
            linhas.append(f"• {s.nome} — R$ {s.preco:.2f}")
        linhas.append("")
        linhas.append("Digite o nome do serviço que deseja agendar.")

        return "\n".join(linhas)

    return None