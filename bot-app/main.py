import asyncio
import hmac
import logging
import re
from contextlib import suppress
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from admin import admin_app
from conversa import processar_mensagem
from core.config import settings
from core.database import SessionLocal, get_db
from core.models import Empresa, UsuarioPainel
from core.rate_limit import excedeu_limite, ip_do_cliente
from core.redis_client import redis_cliente
from core.schema import ensure_schema
from services.configuracoes import obter_configuracao
from services.lembretes import enviar_lembretes_pendentes
from services.usuarios import PAPEL_OPERADOR, criar_usuario

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax", https_only=True)
app.mount("/admin", admin_app)


def _validar_conta_form(form) -> tuple[dict[str, str], dict]:
    erros: dict[str, str] = {}
    dados = {
        "nome": (form.get("nome") or "").strip(),
        "email": (form.get("email") or "").strip().lower(),
    }
    senha = form.get("senha") or ""

    if not dados["nome"]:
        erros["nome"] = "Informe seu nome."
    if not dados["email"]:
        erros["email"] = "Informe seu e-mail."
    elif not EMAIL_REGEX.fullmatch(dados["email"]):
        erros["email"] = "Informe um e-mail válido."
    if len(senha) < 8:
        erros["senha"] = "A senha deve ter pelo menos 8 caracteres."

    dados["senha"] = senha
    return erros, dados


def _autenticar_sessao(request: Request, usuario: UsuarioPainel) -> None:
    request.session["admin_authenticated"] = True
    request.session["admin_username"] = usuario.nome
    request.session["is_superadmin"] = False
    request.session["usuario_id"] = usuario.id
    request.session["usuario_empresa_id"] = usuario.empresa_id
    request.session["usuario_papel"] = usuario.papel


@app.get("/", response_class=HTMLResponse)
async def raiz(request: Request):
    if request.session.get("admin_authenticated"):
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return templates.TemplateResponse(request, "site/landing.html", {"request": request})


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_inicio(request: Request):
    if request.session.get("admin_authenticated"):
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    return templates.TemplateResponse(
        request,
        "onboarding/setup.html",
        {"request": request, "title": "Criar minha conta", "draft": {}, "errors": {}},
    )


@app.post("/onboarding")
async def onboarding_submit(request: Request, db: Session = Depends(get_db)):
    if excedeu_limite(f"ratelimit:onboarding:{ip_do_cliente(request)}", limite=5, janela_segundos=60):
        return templates.TemplateResponse(
            request,
            "onboarding/setup.html",
            {
                "request": request,
                "title": "Criar minha conta",
                "draft": {},
                "errors": {"geral": "Muitas tentativas. Aguarde um minuto e tente novamente."},
            },
            status_code=429,
        )

    form = await request.form()
    erros, dados = _validar_conta_form(form)

    if not erros.get("email") and db.query(UsuarioPainel.id).filter_by(email=dados["email"]).first():
        erros["email"] = "Já existe uma conta com esse e-mail."

    if erros:
        return templates.TemplateResponse(
            request,
            "onboarding/setup.html",
            {"request": request, "title": "Criar minha conta", "draft": dados, "errors": erros},
            status_code=400,
        )

    try:
        usuario = criar_usuario(db, nome=dados["nome"], email=dados["email"], senha=dados["senha"], papel=PAPEL_OPERADOR)
    except IntegrityError:
        db.rollback()
        logger.exception("Conflito ao criar conta (email=%s)", dados["email"])
        return templates.TemplateResponse(
            request,
            "onboarding/setup.html",
            {
                "request": request,
                "title": "Criar minha conta",
                "draft": dados,
                "errors": {"email": "Já existe uma conta com esse e-mail."},
            },
            status_code=400,
        )

    _autenticar_sessao(request, usuario)

    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        redis_cliente.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="servico_indisponivel") from exc

    return {"status": "ok", "database": "ok", "redis": "ok"}


_lembretes_task: asyncio.Task | None = None


async def _loop_lembretes():
    while True:
        db = SessionLocal()
        intervalo_minutos = settings.lembrete_intervalo_minutos
        try:
            intervalo_minutos = obter_configuracao(db).lembrete_intervalo_minutos
            enviados = await enviar_lembretes_pendentes(db)
            if enviados:
                logger.info("Lembretes automáticos enviados: %s", enviados)
        except Exception:
            logger.exception("Falha no ciclo de lembretes automáticos")
        finally:
            db.close()
        await asyncio.sleep(intervalo_minutos * 60)


@app.on_event("startup")
async def startup_schema():
    ensure_schema()
    global _lembretes_task
    _lembretes_task = asyncio.create_task(_loop_lembretes())


@app.on_event("shutdown")
async def shutdown_lembretes():
    if _lembretes_task is not None:
        _lembretes_task.cancel()
        with suppress(asyncio.CancelledError):
            await _lembretes_task


def extrair_conteudo(dados: dict) -> tuple[str | None, str | None]:
    """Retorna (texto, id_interacao).

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
async def receber_mensagem(request: Request, db: Session = Depends(get_db)):
    token = request.query_params.get("token") or ""
    if not hmac.compare_digest(token, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="token_invalido")

    payload = await request.json()
    logger.debug("Payload recebido no webhook: %s", payload)
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

    empresa = db.query(Empresa).filter_by(
        evolution_instance_name=nome_instancia, ativo=True
    ).first()

    if not empresa:
        return {"status": "empresa_nao_encontrada"}

    # id_interacao tem prioridade sobre o texto quando existe (clique é mais preciso que texto)
    await processar_mensagem(db, empresa, numero, texto, id_interacao)

    return {"status": "ok"}
