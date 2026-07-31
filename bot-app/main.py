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