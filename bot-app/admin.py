from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from config import settings
from database import SessionLocal
from models import Agendamento, ClienteFinal, Empresa, Servico

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
admin_app = FastAPI()
admin_app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax", https_only=False)


def admin_required(request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def page_context(request: Request, **kwargs):
    contexto = {
        "request": request,
        "admin_username": request.session.get("admin_username"),
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }
    contexto.update(kwargs)
    return contexto


def parse_bool(value):
    return str(value).lower() in {"1", "true", "yes", "on"}


def parse_optional_float(value):
    texto = (value or "").strip()
    if not texto:
        return None
    return float(texto.replace(",", "."))


def form_namespace(form, **overrides):
    dados = {
        "nome": (form.get("nome") or "").strip(),
        "slug": (form.get("slug") or "").strip(),
        "segmento": (form.get("segmento") or "").strip(),
        "telefone_whatsapp": (form.get("telefone_whatsapp") or "").strip(),
        "evolution_instance_name": (form.get("evolution_instance_name") or "").strip(),
        "horario_abertura": (form.get("horario_abertura") or "08:00").strip(),
        "horario_fechamento": (form.get("horario_fechamento") or "18:00").strip(),
        "intervalo_entre_atendimentos_minutos": int(form.get("intervalo_entre_atendimentos_minutos") or 15),
        "dias_indisponiveis": (form.get("dias_indisponiveis") or "").strip(),
        "datas_indisponiveis": (form.get("datas_indisponiveis") or "").strip(),
        "duracao_minutos": int(form.get("duracao_minutos") or 30),
        "preco": parse_optional_float(form.get("preco")),
        "empresa_id": int(form.get("empresa_id")) if form.get("empresa_id") else None,
        "ativo": parse_bool(form.get("ativo")),
    }
    dados.update(overrides)
    return SimpleNamespace(**dados)


def load_empresa(db, empresa_id: int):
    empresa = db.query(Empresa).filter(Empresa.id == empresa_id).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="empresa_nao_encontrada")
    return empresa


def load_servico(db, servico_id: int):
    servico = db.query(Servico).filter(Servico.id == servico_id).first()
    if not servico:
        raise HTTPException(status_code=404, detail="servico_nao_encontrado")
    return servico


@admin_app.get("", include_in_schema=False)
@admin_app.get("/")
async def admin_root(request: Request):
    if not request.session.get("admin_authenticated"):
        return RedirectResponse(url="/admin/login", status_code=303)
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@admin_app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("admin_authenticated"):
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        page_context(request, title="Entrar"),
    )


@admin_app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""

    if username != settings.admin_username or password != settings.admin_password:
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            page_context(request, title="Entrar", error="Usuário ou senha inválidos."),
            status_code=401,
        )

    request.session["admin_authenticated"] = True
    request.session["admin_username"] = username
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@admin_app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@admin_app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        total_empresas = db.query(func.count(Empresa.id)).scalar() or 0
        empresas_ativas = db.query(func.count(Empresa.id)).filter(Empresa.ativo.is_(True)).scalar() or 0
        total_servicos = db.query(func.count(Servico.id)).scalar() or 0
        total_agendamentos = db.query(func.count(Agendamento.id)).scalar() or 0
        recentes = (
            db.query(Agendamento, Empresa.nome.label("empresa_nome"), Servico.nome.label("servico_nome"), ClienteFinal.nome.label("cliente_nome"), ClienteFinal.telefone.label("cliente_telefone"))
            .join(Empresa, Agendamento.empresa_id == Empresa.id)
            .join(Servico, Agendamento.servico_id == Servico.id)
            .join(ClienteFinal, Agendamento.cliente_final_id == ClienteFinal.id)
            .order_by(Agendamento.data_hora.desc())
            .limit(8)
            .all()
        )
    finally:
        db.close()

    agendamentos = [
        {
            "id": item.Agendamento.id,
            "empresa_nome": item.empresa_nome,
            "servico_nome": item.servico_nome,
            "cliente_nome": item.cliente_nome,
            "cliente_telefone": item.cliente_telefone,
            "data_hora": item.Agendamento.data_hora,
            "status": item.Agendamento.status,
        }
        for item in recentes
    ]

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        page_context(
            request,
            title="Dashboard",
            total_empresas=total_empresas,
            empresas_ativas=empresas_ativas,
            total_servicos=total_servicos,
            total_agendamentos=total_agendamentos,
            agendamentos=agendamentos,
        ),
    )


@admin_app.get("/empresas", response_class=HTMLResponse)
async def empresas_list(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
        servicos_count = dict(
            db.query(Servico.empresa_id, func.count(Servico.id))
            .group_by(Servico.empresa_id)
            .all()
        )
        agendamentos_count = dict(
            db.query(Agendamento.empresa_id, func.count(Agendamento.id))
            .group_by(Agendamento.empresa_id)
            .all()
        )
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/empresas_list.html",
        page_context(
            request,
            title="Empresas",
            empresas=empresas,
            servicos_count=servicos_count,
            agendamentos_count=agendamentos_count,
        ),
    )


@admin_app.get("/empresas/nova", response_class=HTMLResponse)
async def empresa_new_page(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response
    return templates.TemplateResponse(
        request,
        "admin/empresa_form.html",
        page_context(request, title="Nova empresa", empresa=None, action_url="/admin/empresas/nova"),
    )


@admin_app.post("/empresas/nova")
async def empresa_new_submit(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    db = SessionLocal()
    try:
        empresa = Empresa(
            nome=(form.get("nome") or "").strip(),
            slug=(form.get("slug") or "").strip(),
            segmento=(form.get("segmento") or "").strip(),
            telefone_whatsapp=(form.get("telefone_whatsapp") or "").strip(),
            evolution_instance_name=(form.get("evolution_instance_name") or "").strip(),
            horario_abertura=(form.get("horario_abertura") or "08:00").strip(),
            horario_fechamento=(form.get("horario_fechamento") or "18:00").strip(),
            intervalo_entre_atendimentos_minutos=int(form.get("intervalo_entre_atendimentos_minutos") or 15),
            dias_indisponiveis=(form.get("dias_indisponiveis") or "").strip(),
            datas_indisponiveis=(form.get("datas_indisponiveis") or "").strip(),
            ativo=parse_bool(form.get("ativo")),
        )
        db.add(empresa)
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "admin/empresa_form.html",
            page_context(
                request,
                title="Nova empresa",
                empresa=form_namespace(form),
                action_url="/admin/empresas/nova",
                error="Já existe uma empresa com esse slug.",
            ),
            status_code=400,
        )
    finally:
        db.close()

    return RedirectResponse(url="/admin/empresas?message=Empresa criada com sucesso.", status_code=303)


@admin_app.get("/empresas/{empresa_id}/editar", response_class=HTMLResponse)
async def empresa_edit_page(request: Request, empresa_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        empresa = load_empresa(db, empresa_id)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/empresa_form.html",
        page_context(
            request,
            title="Editar empresa",
            empresa=empresa,
            action_url=f"/admin/empresas/{empresa_id}/editar",
        ),
    )


@admin_app.post("/empresas/{empresa_id}/editar")
async def empresa_edit_submit(request: Request, empresa_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    db = SessionLocal()
    try:
        empresa = load_empresa(db, empresa_id)
        empresa.nome = (form.get("nome") or "").strip()
        empresa.slug = (form.get("slug") or "").strip()
        empresa.segmento = (form.get("segmento") or "").strip()
        empresa.telefone_whatsapp = (form.get("telefone_whatsapp") or "").strip()
        empresa.evolution_instance_name = (form.get("evolution_instance_name") or "").strip()
        empresa.horario_abertura = (form.get("horario_abertura") or "08:00").strip()
        empresa.horario_fechamento = (form.get("horario_fechamento") or "18:00").strip()
        empresa.intervalo_entre_atendimentos_minutos = int(form.get("intervalo_entre_atendimentos_minutos") or 15)
        empresa.dias_indisponiveis = (form.get("dias_indisponiveis") or "").strip()
        empresa.datas_indisponiveis = (form.get("datas_indisponiveis") or "").strip()
        empresa.ativo = parse_bool(form.get("ativo"))
        db.commit()
    except IntegrityError:
        db.rollback()
        empresa = load_empresa(db, empresa_id)
        return templates.TemplateResponse(
            request,
            "admin/empresa_form.html",
            page_context(
                request,
                title="Editar empresa",
                empresa=empresa,
                action_url=f"/admin/empresas/{empresa_id}/editar",
                error="Já existe uma empresa com esse slug.",
            ),
            status_code=400,
        )
    finally:
        db.close()

    return RedirectResponse(url="/admin/empresas?message=Empresa atualizada com sucesso.", status_code=303)


@admin_app.post("/empresas/{empresa_id}/toggle")
async def empresa_toggle(request: Request, empresa_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        empresa = load_empresa(db, empresa_id)
        empresa.ativo = not empresa.ativo
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/admin/empresas?message=Status da empresa atualizado.", status_code=303)


@admin_app.get("/servicos", response_class=HTMLResponse)
async def servicos_list(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = request.query_params.get("empresa_id")
    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
        query = db.query(Servico).options(joinedload(Servico.empresa)).order_by(Servico.nome.asc())
        if empresa_id:
            query = query.filter(Servico.empresa_id == int(empresa_id))
        servicos = query.all()
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/servicos_list.html",
        page_context(
            request,
            title="Serviços",
            empresas=empresas,
            servicos=servicos,
            selected_empresa_id=int(empresa_id) if empresa_id else None,
        ),
    )


@admin_app.get("/servicos/novo", response_class=HTMLResponse)
async def servico_new_page(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = request.query_params.get("empresa_id")
    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/servico_form.html",
        page_context(
            request,
            title="Novo serviço",
            empresas=empresas,
            servico=None,
            selected_empresa_id=int(empresa_id) if empresa_id else None,
            action_url="/admin/servicos/novo",
        ),
    )


@admin_app.post("/servicos/novo")
async def servico_new_submit(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    empresa_id = int(form.get("empresa_id"))
    db = SessionLocal()
    try:
        load_empresa(db, empresa_id)
        servico = Servico(
            empresa_id=empresa_id,
            nome=(form.get("nome") or "").strip(),
            duracao_minutos=int(form.get("duracao_minutos") or 30),
            preco=parse_optional_float(form.get("preco")),
            ativo=parse_bool(form.get("ativo")),
        )
        db.add(servico)
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Serviço criado com sucesso.", status_code=303)


@admin_app.get("/servicos/{servico_id}/editar", response_class=HTMLResponse)
async def servico_edit_page(request: Request, servico_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
        servico = load_servico(db, servico_id)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/servico_form.html",
        page_context(
            request,
            title="Editar serviço",
            empresas=empresas,
            servico=servico,
            selected_empresa_id=servico.empresa_id,
            action_url=f"/admin/servicos/{servico_id}/editar",
        ),
    )


@admin_app.post("/servicos/{servico_id}/editar")
async def servico_edit_submit(request: Request, servico_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    empresa_id = int(form.get("empresa_id"))
    db = SessionLocal()
    try:
        servico = load_servico(db, servico_id)
        load_empresa(db, empresa_id)
        servico.empresa_id = empresa_id
        servico.nome = (form.get("nome") or "").strip()
        servico.duracao_minutos = int(form.get("duracao_minutos") or 30)
        servico.preco = parse_optional_float(form.get("preco"))
        servico.ativo = parse_bool(form.get("ativo"))
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Serviço atualizado com sucesso.", status_code=303)


@admin_app.post("/servicos/{servico_id}/toggle")
async def servico_toggle(request: Request, servico_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        servico = load_servico(db, servico_id)
        servico.ativo = not servico.ativo
        db.commit()
        empresa_id = servico.empresa_id
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Status do serviço atualizado.", status_code=303)


@admin_app.get("/agendamentos", response_class=HTMLResponse)
async def agendamentos_list(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = request.query_params.get("empresa_id")
    status = request.query_params.get("status")

    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
        query = (
            db.query(Agendamento, Empresa.nome.label("empresa_nome"), Servico.nome.label("servico_nome"), ClienteFinal.nome.label("cliente_nome"), ClienteFinal.telefone.label("cliente_telefone"))
            .join(Empresa, Agendamento.empresa_id == Empresa.id)
            .join(Servico, Agendamento.servico_id == Servico.id)
            .join(ClienteFinal, Agendamento.cliente_final_id == ClienteFinal.id)
            .order_by(Agendamento.data_hora.desc())
        )
        if empresa_id:
            query = query.filter(Agendamento.empresa_id == int(empresa_id))
        if status:
            query = query.filter(Agendamento.status == status)
        registros = query.all()
    finally:
        db.close()

    agendamentos = [
        {
            "id": item.Agendamento.id,
            "empresa_nome": item.empresa_nome,
            "servico_nome": item.servico_nome,
            "cliente_nome": item.cliente_nome,
            "cliente_telefone": item.cliente_telefone,
            "data_hora": item.Agendamento.data_hora,
            "status": item.Agendamento.status,
        }
        for item in registros
    ]

    return templates.TemplateResponse(
        request,
        "admin/agendamentos_list.html",
        page_context(
            request,
            title="Agendamentos",
            empresas=empresas,
            agendamentos=agendamentos,
            selected_empresa_id=int(empresa_id) if empresa_id else None,
            selected_status=status or "",
        ),
    )