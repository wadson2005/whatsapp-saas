import asyncio
import logging
import re
from contextlib import suppress
from datetime import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from admin import admin_app, parse_optional_float
from config import settings
from configuracoes import obter_configuracao
from conversa import processar_mensagem
from database import SessionLocal
from lembretes import enviar_lembretes_pendentes
from models import Empresa, Servico
from redis_client import redis_cliente
from schema import ensure_schema

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SLUG_REGEX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax", https_only=False)
app.mount("/admin", admin_app)

EVOLUTION_URL = settings.evolution_url
EVOLUTION_API_KEY = settings.evolution_api_key


def _empresa_cadastrada(db) -> bool:
    return db.query(Empresa.id).first() is not None


def _normalizar_telefone(telefone: str | None) -> str:
    return re.sub(r"\D+", "", telefone or "")


def _parse_horario(texto: str) -> time:
    texto_limpo = (texto or "").strip()
    if not texto_limpo:
        raise ValueError("Informe um horário no formato HH:MM.")
    try:
        return time.fromisoformat(texto_limpo)
    except ValueError as exc:
        raise ValueError("Informe um horário válido no formato HH:MM.") from exc


def _contexto_onboarding(request: Request, **kwargs):
    contexto = {
        "request": request,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }
    contexto.update(kwargs)
    return contexto


def _draft_onboarding(request: Request) -> dict:
    return dict(request.session.get("onboarding_draft", {}))


def _save_onboarding_draft(request: Request, data: dict) -> None:
    request.session["onboarding_draft"] = data


def _clear_onboarding_draft(request: Request) -> None:
    request.session.pop("onboarding_draft", None)


def _save_onboarding_result(request: Request, data: dict) -> None:
    request.session["onboarding_result"] = data


def _draft_error_response(request: Request, template_name: str, status_code: int, **kwargs):
    return templates.TemplateResponse(
        request,
        template_name,
        _contexto_onboarding(request, **kwargs),
        status_code=status_code,
    )


def _redirecionar_inicio(request: Request) -> RedirectResponse:
    if request.session.get("admin_authenticated"):
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    db = SessionLocal()
    try:
        existe_empresa = _empresa_cadastrada(db)
    finally:
        db.close()

    return RedirectResponse(url="/admin/login" if existe_empresa else "/onboarding", status_code=303)


def _validar_empresa_form(nome: str, slug: str, segmento: str) -> dict[str, str]:
    erros: dict[str, str] = {}
    if not nome:
        erros["nome"] = "Informe o nome da empresa."
    if not slug:
        erros["slug"] = "Informe um slug."
    elif not SLUG_REGEX.fullmatch(slug):
        erros["slug"] = "Use apenas letras minúsculas, números e hífens."
    if not segmento:
        erros["segmento"] = "Informe o segmento."
    return erros


def _validar_configuracao_form(form) -> tuple[dict[str, str], dict]:
    erros: dict[str, str] = {}
    dados = {
        "telefone_whatsapp": _normalizar_telefone(form.get("telefone_whatsapp")),
        "evolution_instance_name": (form.get("evolution_instance_name") or "").strip(),
        "horario_abertura": (form.get("horario_abertura") or "08:00").strip(),
        "horario_fechamento": (form.get("horario_fechamento") or "18:00").strip(),
        "intervalo_entre_atendimentos_minutos": (form.get("intervalo_entre_atendimentos_minutos") or "15").strip(),
        "primeiro_servico_nome": (form.get("primeiro_servico_nome") or "").strip(),
        "primeiro_servico_duracao_minutos": (form.get("primeiro_servico_duracao_minutos") or "30").strip(),
        "primeiro_servico_preco": (form.get("primeiro_servico_preco") or "").strip(),
    }

    if len(dados["telefone_whatsapp"]) < 10:
        erros["telefone_whatsapp"] = "Informe um telefone WhatsApp válido."
    if not dados["evolution_instance_name"]:
        erros["evolution_instance_name"] = "Informe a instância do WhatsApp."
    if not dados["primeiro_servico_nome"]:
        erros["primeiro_servico_nome"] = "Informe o primeiro serviço."

    try:
        horario_abertura = _parse_horario(dados["horario_abertura"])
    except ValueError as exc:
        erros["horario_abertura"] = str(exc)
        horario_abertura = None

    try:
        horario_fechamento = _parse_horario(dados["horario_fechamento"])
    except ValueError as exc:
        erros["horario_fechamento"] = str(exc)
        horario_fechamento = None

    try:
        intervalo = int(dados["intervalo_entre_atendimentos_minutos"])
        if intervalo < 0:
            raise ValueError
    except ValueError:
        erros["intervalo_entre_atendimentos_minutos"] = "Informe um intervalo válido em minutos."
        intervalo = None

    try:
        duracao = int(dados["primeiro_servico_duracao_minutos"])
        if duracao < 1:
            raise ValueError
    except ValueError:
        erros["primeiro_servico_duracao_minutos"] = "Informe uma duração válida em minutos."
        duracao = None

    preco = None
    if dados["primeiro_servico_preco"]:
        try:
            preco = parse_optional_float(dados["primeiro_servico_preco"])
        except ValueError:
            erros["primeiro_servico_preco"] = "Informe um preço válido."

    if horario_abertura and horario_fechamento and horario_abertura >= horario_fechamento:
        erros["horario_fechamento"] = "O fechamento precisa ser depois da abertura."

    dados["horario_abertura"] = horario_abertura.strftime("%H:%M") if horario_abertura else dados["horario_abertura"]
    dados["horario_fechamento"] = horario_fechamento.strftime("%H:%M") if horario_fechamento else dados["horario_fechamento"]
    dados["intervalo_entre_atendimentos_minutos"] = intervalo
    dados["primeiro_servico_duracao_minutos"] = duracao
    dados["primeiro_servico_preco"] = preco
    return erros, dados


@app.get("/", response_class=HTMLResponse)
async def raiz(request: Request):
    return _redirecionar_inicio(request)


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_inicio(request: Request):
    db = SessionLocal()
    try:
        if _empresa_cadastrada(db):
            return RedirectResponse(url="/admin/login?message=Sua empresa já está configurada.", status_code=303)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "onboarding/setup.html",
        _contexto_onboarding(
            request,
            title="Configurar empresa",
            step=1,
            draft=_draft_onboarding(request),
            errors={},
        ),
    )


@app.post("/onboarding")
async def onboarding_empresa_submit(request: Request):
    form = await request.form()
    nome = (form.get("nome") or "").strip()
    slug = (form.get("slug") or "").strip().lower()
    segmento = (form.get("segmento") or "").strip()

    erros = _validar_empresa_form(nome, slug, segmento)

    db = SessionLocal()
    try:
        if slug and db.query(Empresa.id).filter(Empresa.slug == slug).first():
            erros["slug"] = "Já existe uma empresa com esse slug."
    finally:
        db.close()

    if erros:
        return _draft_error_response(
            request,
            "onboarding/setup.html",
            400,
            title="Configurar empresa",
            step=1,
            draft={"nome": nome, "slug": slug, "segmento": segmento},
            errors=erros,
        )

    _save_onboarding_draft(request, {"nome": nome, "slug": slug, "segmento": segmento})
    return RedirectResponse(url="/onboarding/configurar", status_code=303)


@app.get("/onboarding/configurar", response_class=HTMLResponse)
async def onboarding_configurar(request: Request):
    draft = _draft_onboarding(request)
    if not draft:
        return RedirectResponse(url="/onboarding", status_code=303)

    return templates.TemplateResponse(
        request,
        "onboarding/setup.html",
        _contexto_onboarding(
            request,
            title="Configuração do WhatsApp",
            step=2,
            draft=draft,
            errors={},
        ),
    )


@app.post("/onboarding/configurar")
async def onboarding_configurar_submit(request: Request):
    draft = _draft_onboarding(request)
    if not draft:
        return RedirectResponse(url="/onboarding", status_code=303)

    form = await request.form()
    erros, dados = _validar_configuracao_form(form)

    db = SessionLocal()
    try:
        if dados["evolution_instance_name"] and db.query(Empresa.id).filter(
            Empresa.evolution_instance_name == dados["evolution_instance_name"]
        ).first():
            erros["evolution_instance_name"] = "Já existe uma empresa com essa instância."

        if erros:
            return _draft_error_response(
                request,
                "onboarding/setup.html",
                400,
                title="Configuração do WhatsApp",
                step=2,
                draft={**draft, **dados},
                errors=erros,
            )

        empresa = Empresa(
            nome=draft["nome"],
            slug=draft["slug"],
            segmento=draft["segmento"],
            telefone_whatsapp=dados["telefone_whatsapp"],
            evolution_instance_name=dados["evolution_instance_name"],
            horario_abertura=dados["horario_abertura"],
            horario_fechamento=dados["horario_fechamento"],
            intervalo_entre_atendimentos_minutos=dados["intervalo_entre_atendimentos_minutos"],
            ativo=True,
        )
        db.add(empresa)
        db.flush()

        servico = Servico(
            empresa_id=empresa.id,
            nome=dados["primeiro_servico_nome"],
            duracao_minutos=dados["primeiro_servico_duracao_minutos"],
            preco=dados["primeiro_servico_preco"],
            ativo=True,
        )
        db.add(servico)
        db.commit()
        db.refresh(empresa)
    except IntegrityError:
        db.rollback()
        return _draft_error_response(
            request,
            "onboarding/setup.html",
            400,
            title="Configuração do WhatsApp",
            step=2,
            draft={**draft, **dados},
            errors={"geral": "Não foi possível salvar a empresa com esses dados."},
        )
    finally:
        db.close()

    _clear_onboarding_draft(request)
    _save_onboarding_result(
        request,
        {
            "empresa_nome": empresa.nome,
            "servico_nome": dados["primeiro_servico_nome"],
            "admin_url": "/admin/login",
        },
    )
    return RedirectResponse(url="/onboarding/sucesso", status_code=303)


@app.get("/onboarding/sucesso", response_class=HTMLResponse)
async def onboarding_sucesso(request: Request):
    resultado = request.session.get("onboarding_result")
    if not resultado:
        return RedirectResponse(url="/onboarding", status_code=303)

    return templates.TemplateResponse(
        request,
        "onboarding/success.html",
        _contexto_onboarding(request, title="Configuração concluída", result=resultado),
    )


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