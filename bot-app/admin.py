from pathlib import Path
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from atendimento_humano import STATUS_EM_ATENDIMENTO, STATUS_FINALIZADO, STATUS_PENDENTE, atualizar_status_solicitacao_atendimento
from config import settings
from conhecimento import atualizar_conhecimento, criar_conhecimento, excluir_conhecimento, listar_conhecimento
from database import SessionLocal
from metricas import calcular_metricas, gerar_insights, listar_clientes_inativos
from models import Agendamento, ClienteFinal, Empresa, EmpresaConhecimento, Servico, SolicitacaoAtendimento

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


def parse_optional_int(value):
    texto = (value or "").strip()
    if not texto:
        return None
    return int(texto)


def parse_optional_time(value):
    texto = (value or "").strip()
    if not texto:
        return None
    return texto


def parse_optional_str(value):
    texto = (value or "").strip()
    return texto or None


def _query_empresa_id(request: Request):
    valor = request.query_params.get("empresa_id")
    if valor in {None, ""}:
        return None
    return int(valor)


def _base_empresa_contexto(db, empresa_id: int | None):
    empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
    empresa = None
    if empresa_id:
        empresa = db.query(Empresa).filter_by(id=empresa_id).first()
    elif len(empresas) == 1:
        empresa = empresas[0]
    return empresas, empresa


def _empresa_query_ids(empresa_id: int | None):
    if empresa_id is None:
        return None
    return int(empresa_id)


def _status_agendamento_rotulo(status: str) -> str:
    mapa = {
        "agendado": "Agendado",
        "confirmado": "Confirmado",
        "concluido": "Concluído",
        "cancelado": "Cancelado",
        "pendente": "Agendado",
        "realizado": "Concluído",
    }
    return mapa.get(status, status.title() if status else "-")


def _agendamento_status_permitido(status: str) -> bool:
    return status in {"agendado", "confirmado", "concluido", "cancelado"}


def form_namespace(form, **overrides):
    dados = {
        "nome": (form.get("nome") or "").strip(),
        "slug": (form.get("slug") or "").strip(),
        "segmento": (form.get("segmento") or "").strip(),
        "telefone_whatsapp": (form.get("telefone_whatsapp") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "endereco": (form.get("endereco") or "").strip(),
        "descricao": (form.get("descricao") or "").strip(),
        "logo_url": (form.get("logo_url") or "").strip(),
        "evolution_instance_name": (form.get("evolution_instance_name") or "").strip(),
        "horario_abertura": (form.get("horario_abertura") or "08:00").strip(),
        "horario_fechamento": (form.get("horario_fechamento") or "18:00").strip(),
        "horario_almoco_inicio": (form.get("horario_almoco_inicio") or "").strip(),
        "horario_almoco_fim": (form.get("horario_almoco_fim") or "").strip(),
        "dias_funcionamento": (form.get("dias_funcionamento") or "0,1,2,3,4,5").strip(),
        "intervalo_entre_atendimentos_minutos": int(form.get("intervalo_entre_atendimentos_minutos") or 15),
        "dias_indisponiveis": (form.get("dias_indisponiveis") or "").strip(),
        "datas_indisponiveis": (form.get("datas_indisponiveis") or "").strip(),
        "atendimento_automatico_ativo": parse_bool(form.get("atendimento_automatico_ativo")),
        "permitir_atendimento_humano": parse_bool(form.get("permitir_atendimento_humano")),
        "horario_resposta_inicio": (form.get("horario_resposta_inicio") or "08:00").strip(),
        "horario_resposta_fim": (form.get("horario_resposta_fim") or "18:00").strip(),
        "mensagem_fora_horario": (form.get("mensagem_fora_horario") or "").strip(),
        "tempo_max_conversa_minutos": parse_optional_int(form.get("tempo_max_conversa_minutos")) or 120,
        "tempo_expiracao_contexto_minutos": parse_optional_int(form.get("tempo_expiracao_contexto_minutos")) or 30,
        "mensagem_boas_vindas": (form.get("mensagem_boas_vindas") or "").strip(),
        "mensagem_encerramento": (form.get("mensagem_encerramento") or "").strip(),
        "mensagem_atendimento_humano": (form.get("mensagem_atendimento_humano") or "").strip(),
        "mensagem_sem_horarios": (form.get("mensagem_sem_horarios") or "").strip(),
        "mensagem_confirmacao": (form.get("mensagem_confirmacao") or "").strip(),
        "duracao_minutos": int(form.get("duracao_minutos") or 30),
        "ordem_exibicao": parse_optional_int(form.get("ordem_exibicao")) or 0,
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


def load_cliente(db, cliente_id: int):
    cliente = db.query(ClienteFinal).filter(ClienteFinal.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="cliente_nao_encontrado")
    return cliente


def load_agendamento(db, agendamento_id: int):
    agendamento = db.query(Agendamento).filter(Agendamento.id == agendamento_id).first()
    if not agendamento:
        raise HTTPException(status_code=404, detail="agendamento_nao_encontrado")
    return agendamento


def load_conhecimento(db, conhecimento_id: int):
    entrada = db.query(EmpresaConhecimento).filter(EmpresaConhecimento.id == conhecimento_id).first()
    if not entrada:
        raise HTTPException(status_code=404, detail="conhecimento_nao_encontrado")
    return entrada


def _aplicar_dados_empresa(empresa: Empresa, dados: SimpleNamespace):
    empresa.nome = dados.nome
    empresa.slug = dados.slug
    empresa.segmento = dados.segmento
    empresa.telefone_whatsapp = dados.telefone_whatsapp
    empresa.email = dados.email
    empresa.endereco = dados.endereco
    empresa.descricao = dados.descricao
    empresa.logo_url = dados.logo_url
    empresa.evolution_instance_name = dados.evolution_instance_name
    empresa.horario_abertura = dados.horario_abertura
    empresa.horario_fechamento = dados.horario_fechamento
    empresa.horario_almoco_inicio = dados.horario_almoco_inicio
    empresa.horario_almoco_fim = dados.horario_almoco_fim
    empresa.dias_funcionamento = dados.dias_funcionamento
    empresa.intervalo_entre_atendimentos_minutos = dados.intervalo_entre_atendimentos_minutos
    empresa.dias_indisponiveis = dados.dias_indisponiveis
    empresa.datas_indisponiveis = dados.datas_indisponiveis
    empresa.atendimento_automatico_ativo = dados.atendimento_automatico_ativo
    empresa.permitir_atendimento_humano = dados.permitir_atendimento_humano
    empresa.horario_resposta_inicio = dados.horario_resposta_inicio
    empresa.horario_resposta_fim = dados.horario_resposta_fim
    empresa.mensagem_fora_horario = dados.mensagem_fora_horario
    empresa.tempo_max_conversa_minutos = dados.tempo_max_conversa_minutos
    empresa.tempo_expiracao_contexto_minutos = dados.tempo_expiracao_contexto_minutos
    empresa.mensagem_boas_vindas = dados.mensagem_boas_vindas
    empresa.mensagem_encerramento = dados.mensagem_encerramento
    empresa.mensagem_atendimento_humano = dados.mensagem_atendimento_humano
    empresa.mensagem_sem_horarios = dados.mensagem_sem_horarios
    empresa.mensagem_confirmacao = dados.mensagem_confirmacao
    empresa.ativo = dados.ativo


def _aplicar_dados_servico(servico: Servico, dados: SimpleNamespace):
    servico.empresa_id = dados.empresa_id
    servico.nome = dados.nome
    servico.descricao = dados.descricao
    servico.duracao_minutos = dados.duracao_minutos
    servico.preco = dados.preco
    servico.ordem_exibicao = dados.ordem_exibicao
    servico.ativo = dados.ativo


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


def _periodo_dashboard(request: Request) -> tuple[datetime, datetime]:
    agora = datetime.utcnow()
    padrao_inicio = agora - timedelta(days=30)

    data_inicio_str = (request.query_params.get("data_inicio") or "").strip()
    data_fim_str = (request.query_params.get("data_fim") or "").strip()

    try:
        data_inicio = datetime.combine(date.fromisoformat(data_inicio_str), time.min) if data_inicio_str else padrao_inicio
    except ValueError:
        data_inicio = padrao_inicio

    try:
        data_fim = datetime.combine(date.fromisoformat(data_fim_str), time.max) if data_fim_str else agora
    except ValueError:
        data_fim = agora

    return data_inicio, data_fim


@admin_app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = _query_empresa_id(request)
    data_inicio, data_fim = _periodo_dashboard(request)
    db = SessionLocal()
    try:
        empresas, empresa = _base_empresa_contexto(db, empresa_id)
        metricas_periodo = calcular_metricas(db, empresa_id, data_inicio, data_fim)
        filtros_empresa = []
        if empresa_id:
            filtros_empresa.append(Empresa.id == empresa_id)

        total_empresas = db.query(func.count(Empresa.id)).scalar() or 0
        empresas_ativas = db.query(func.count(Empresa.id)).filter(Empresa.ativo.is_(True)).scalar() or 0
        total_clientes = db.query(func.count(ClienteFinal.id)).filter(*([ClienteFinal.empresa_id == empresa_id] if empresa_id else [])).scalar() or 0
        total_servicos = db.query(func.count(Servico.id)).filter(Servico.excluido_em.is_(None), *([Servico.empresa_id == empresa_id] if empresa_id else [])).scalar() or 0
        total_agendamentos = db.query(func.count(Agendamento.id)).filter(*([Agendamento.empresa_id == empresa_id] if empresa_id else [])).scalar() or 0
        total_solicitacoes = db.query(func.count(SolicitacaoAtendimento.id)).filter(
            SolicitacaoAtendimento.status == STATUS_PENDENTE,
            *([SolicitacaoAtendimento.empresa_id == empresa_id] if empresa_id else []),
        ).scalar() or 0
        atendimentos_dia = db.query(func.count(Agendamento.id)).filter(
            *( [Agendamento.empresa_id == empresa_id] if empresa_id else [] ),
            Agendamento.status != "cancelado",
            func.date(Agendamento.data_hora) == datetime.utcnow().date(),
        ).scalar() or 0
        recentes = (
            db.query(
                Agendamento,
                Empresa.nome.label("empresa_nome"),
                Servico.nome.label("servico_nome"),
                ClienteFinal.nome.label("cliente_nome"),
                ClienteFinal.telefone.label("cliente_telefone"),
            )
            .join(Empresa, Agendamento.empresa_id == Empresa.id)
            .join(Servico, Agendamento.servico_id == Servico.id)
            .join(ClienteFinal, Agendamento.cliente_final_id == ClienteFinal.id)
            .filter(*([Agendamento.empresa_id == empresa_id] if empresa_id else []))
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
            "cliente_final_id": item.Agendamento.cliente_final_id,
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
            empresas=empresas,
            selected_empresa_id=empresa.id if empresa else empresa_id,
            total_empresas=total_empresas,
            empresas_ativas=empresas_ativas,
            total_clientes=total_clientes,
            total_servicos=total_servicos,
            total_agendamentos=total_agendamentos,
            total_solicitacoes=total_solicitacoes,
            atendimentos_dia=atendimentos_dia,
            agendamentos=agendamentos,
            metricas=metricas_periodo,
            data_inicio=data_inicio.date().isoformat(),
            data_fim=data_fim.date().isoformat(),
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
        dados = form_namespace(form)
        empresa = Empresa()
        _aplicar_dados_empresa(empresa, dados)
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
        _aplicar_dados_empresa(empresa, form_namespace(form, empresa_id=empresa_id))
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
        query = (
            db.query(Servico)
            .options(joinedload(Servico.empresa))
            .filter(Servico.excluido_em.is_(None))
            .order_by(Servico.ordem_exibicao.asc(), Servico.nome.asc())
        )
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
        dados = form_namespace(form, empresa_id=empresa_id)
        servico = Servico()
        _aplicar_dados_servico(servico, dados)
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
        _aplicar_dados_servico(servico, form_namespace(form, empresa_id=empresa_id))
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


@admin_app.post("/servicos/{servico_id}/excluir")
async def servico_delete(request: Request, servico_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        servico = load_servico(db, servico_id)
        servico.ativo = False
        servico.excluido_em = datetime.utcnow()
        empresa_id = servico.empresa_id
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Serviço excluído com sucesso.", status_code=303)


@admin_app.get("/conhecimento", response_class=HTMLResponse)
async def conhecimento_list(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = request.query_params.get("empresa_id")
    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
        entradas = listar_conhecimento(db, int(empresa_id) if empresa_id else None)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/conhecimento_list.html",
        page_context(
            request,
            title="Base de conhecimento",
            empresas=empresas,
            entradas=entradas,
            selected_empresa_id=int(empresa_id) if empresa_id else None,
        ),
    )


@admin_app.get("/conhecimento/novo", response_class=HTMLResponse)
async def conhecimento_new_page(request: Request):
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
        "admin/conhecimento_form.html",
        page_context(
            request,
            title="Nova pergunta",
            empresas=empresas,
            entrada=None,
            selected_empresa_id=int(empresa_id) if empresa_id else None,
            action_url="/admin/conhecimento/novo",
        ),
    )


@admin_app.post("/conhecimento/novo")
async def conhecimento_new_submit(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    empresa_id = int(form.get("empresa_id"))
    db = SessionLocal()
    try:
        load_empresa(db, empresa_id)
        criar_conhecimento(
            db,
            empresa_id=empresa_id,
            categoria=parse_optional_str(form.get("categoria")),
            pergunta=(form.get("pergunta") or "").strip(),
            resposta=(form.get("resposta") or "").strip(),
            ativo=parse_bool(form.get("ativo")),
        )
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Pergunta criada com sucesso.", status_code=303)


@admin_app.get("/conhecimento/{conhecimento_id}/editar", response_class=HTMLResponse)
async def conhecimento_edit_page(request: Request, conhecimento_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
        entrada = load_conhecimento(db, conhecimento_id)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/conhecimento_form.html",
        page_context(
            request,
            title="Editar pergunta",
            empresas=empresas,
            entrada=entrada,
            selected_empresa_id=entrada.empresa_id,
            action_url=f"/admin/conhecimento/{conhecimento_id}/editar",
        ),
    )


@admin_app.post("/conhecimento/{conhecimento_id}/editar")
async def conhecimento_edit_submit(request: Request, conhecimento_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    empresa_id = int(form.get("empresa_id"))
    db = SessionLocal()
    try:
        entrada = load_conhecimento(db, conhecimento_id)
        load_empresa(db, empresa_id)
        atualizar_conhecimento(
            entrada,
            categoria=parse_optional_str(form.get("categoria")),
            pergunta=(form.get("pergunta") or "").strip(),
            resposta=(form.get("resposta") or "").strip(),
            ativo=parse_bool(form.get("ativo")),
        )
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Pergunta atualizada com sucesso.", status_code=303)


@admin_app.post("/conhecimento/{conhecimento_id}/toggle")
async def conhecimento_toggle(request: Request, conhecimento_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        entrada = load_conhecimento(db, conhecimento_id)
        entrada.ativo = not entrada.ativo
        db.commit()
        empresa_id = entrada.empresa_id
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Status atualizado.", status_code=303)


@admin_app.post("/conhecimento/{conhecimento_id}/excluir")
async def conhecimento_delete(request: Request, conhecimento_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    db = SessionLocal()
    try:
        entrada = load_conhecimento(db, conhecimento_id)
        excluir_conhecimento(entrada)
        empresa_id = entrada.empresa_id
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Pergunta excluída com sucesso.", status_code=303)


@admin_app.get("/agendamentos", response_class=HTMLResponse)
async def agendamentos_list(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = _query_empresa_id(request)
    status = request.query_params.get("status")
    data = (request.query_params.get("data") or "").strip()
    servico_id = request.query_params.get("servico_id")
    cliente_id = request.query_params.get("cliente_id")

    db = SessionLocal()
    try:
        empresas, empresa = _base_empresa_contexto(db, empresa_id)
        servicos = (
            db.query(Servico)
            .filter(Servico.excluido_em.is_(None), *([Servico.empresa_id == empresa_id] if empresa_id else []))
            .order_by(Servico.ordem_exibicao.asc(), Servico.nome.asc())
            .all()
        )
        clientes = (
            db.query(ClienteFinal)
            .filter(*([ClienteFinal.empresa_id == empresa_id] if empresa_id else []))
            .order_by(ClienteFinal.nome.asc())
            .all()
        )
        query = (
            db.query(
                Agendamento,
                Empresa.nome.label("empresa_nome"),
                Servico.nome.label("servico_nome"),
                ClienteFinal.nome.label("cliente_nome"),
                ClienteFinal.telefone.label("cliente_telefone"),
            )
            .join(Empresa, Agendamento.empresa_id == Empresa.id)
            .join(Servico, Agendamento.servico_id == Servico.id)
            .join(ClienteFinal, Agendamento.cliente_final_id == ClienteFinal.id)
            .filter(*([Agendamento.empresa_id == empresa_id] if empresa_id else []))
            .order_by(Agendamento.data_hora.desc())
        )
        if status:
            query = query.filter(Agendamento.status == status)
        if data:
            try:
                data_obj = datetime.fromisoformat(data).date()
            except ValueError:
                data_obj = None
            if data_obj:
                query = query.filter(func.date(Agendamento.data_hora) == data_obj)
        if servico_id:
            query = query.filter(Agendamento.servico_id == int(servico_id))
        if cliente_id:
            query = query.filter(Agendamento.cliente_final_id == int(cliente_id))
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
            "cliente_final_id": item.Agendamento.cliente_final_id,
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
            servicos=servicos,
            clientes=clientes,
            agendamentos=agendamentos,
            selected_empresa_id=empresa.id if empresa else empresa_id,
            selected_status=status or "",
            selected_data=data,
            selected_servico_id=int(servico_id) if servico_id else None,
            selected_cliente_id=int(cliente_id) if cliente_id else None,
        ),
    )


@admin_app.post("/agendamentos/{agendamento_id}/status")
async def agendamento_status_submit(request: Request, agendamento_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    status = (form.get("status") or "").strip()
    if not _agendamento_status_permitido(status):
        raise HTTPException(status_code=400, detail="status_invalido")

    empresa_id = request.query_params.get("empresa_id") or form.get("empresa_id")
    db = SessionLocal()
    try:
        query = db.query(Agendamento).filter_by(id=agendamento_id)
        if empresa_id:
            query = query.filter(Agendamento.empresa_id == int(empresa_id))
        agendamento = query.first()
        if not agendamento:
            raise HTTPException(status_code=404, detail="agendamento_nao_encontrado")

        agendamento.status = status
        if status == "cancelado":
            agendamento.cancelado_em = datetime.utcnow()
        db.commit()
        empresa_id = agendamento.empresa_id
    finally:
        db.close()

    return RedirectResponse(url=f"/admin/agendamentos?empresa_id={empresa_id}&message=Agendamento atualizado com sucesso.", status_code=303)


@admin_app.get("/clientes", response_class=HTMLResponse)
async def clientes_list(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = _query_empresa_id(request)
    q = (request.query_params.get("q") or "").strip()
    sort = (request.query_params.get("sort") or "recente").strip()

    db = SessionLocal()
    try:
        empresas, empresa = _base_empresa_contexto(db, empresa_id)
        query = (
            db.query(
                ClienteFinal,
                Empresa.nome.label("empresa_nome"),
                func.count(Agendamento.id).label("agendamentos_count"),
                func.max(Agendamento.data_hora).label("ultimo_atendimento"),
            )
            .join(Empresa, ClienteFinal.empresa_id == Empresa.id)
            .outerjoin(Agendamento, Agendamento.cliente_final_id == ClienteFinal.id)
            .filter(*([ClienteFinal.empresa_id == empresa_id] if empresa_id else []))
            .group_by(ClienteFinal.id, Empresa.nome)
        )
        if q:
            like = f"%{q}%"
            query = query.filter(or_(ClienteFinal.nome.ilike(like), ClienteFinal.telefone.ilike(like)))

        if sort == "nome":
            query = query.order_by(ClienteFinal.nome.asc())
        elif sort == "agendamentos":
            query = query.order_by(func.count(Agendamento.id).desc(), ClienteFinal.nome.asc())
        elif sort == "ultimo":
            query = query.order_by(func.max(Agendamento.data_hora).desc().nullslast())
        else:
            query = query.order_by(ClienteFinal.criado_em.desc())

        registros = query.all()
    finally:
        db.close()

    clientes = [
        {
            "id": item.ClienteFinal.id,
            "empresa_id": item.ClienteFinal.empresa_id,
            "empresa_nome": item.empresa_nome,
            "nome": item.ClienteFinal.nome,
            "telefone": item.ClienteFinal.telefone,
            "agendamentos_count": item.agendamentos_count,
            "ultimo_atendimento": item.ultimo_atendimento,
            "criado_em": item.ClienteFinal.criado_em,
        }
        for item in registros
    ]

    return templates.TemplateResponse(
        request,
        "admin/clientes_list.html",
        page_context(
            request,
            title="Clientes",
            empresas=empresas,
            clientes=clientes,
            selected_empresa_id=empresa.id if empresa else empresa_id,
            selected_sort=sort,
            search_q=q,
        ),
    )


@admin_app.get("/clientes/{cliente_id}", response_class=HTMLResponse)
async def cliente_detail(request: Request, cliente_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = _query_empresa_id(request)
    db = SessionLocal()
    try:
        cliente = load_cliente(db, cliente_id)
        if empresa_id and cliente.empresa_id != empresa_id:
            raise HTTPException(status_code=404, detail="cliente_nao_encontrado")

        empresa = load_empresa(db, cliente.empresa_id)
        agendamentos = (
            db.query(Agendamento)
            .options(joinedload(Agendamento.servico))
            .filter_by(cliente_final_id=cliente.id)
            .order_by(Agendamento.data_hora.desc())
            .all()
        )
        solicitacoes = (
            db.query(SolicitacaoAtendimento)
            .filter_by(cliente_id=cliente.id)
            .order_by(SolicitacaoAtendimento.criado_em.desc())
            .all()
        )
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/cliente_detail.html",
        page_context(
            request,
            title=f"Cliente - {cliente.nome or cliente.telefone}",
            cliente=cliente,
            empresa=empresa,
            agendamentos=agendamentos,
            solicitacoes=solicitacoes,
        ),
    )


@admin_app.get("/solicitacoes-atendimento", response_class=HTMLResponse)
async def solicitacoes_atendimento_list(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = request.query_params.get("empresa_id")
    if empresa_id == "":
        empresa_id = None

    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
        if not empresa_id and len(empresas) == 1:
            empresa_id = str(empresas[0].id)

        registros = []
        if empresa_id:
            query = (
                db.query(
                    SolicitacaoAtendimento,
                    Empresa.nome.label("empresa_nome"),
                    ClienteFinal.nome.label("cliente_nome"),
                    ClienteFinal.telefone.label("cliente_telefone"),
                )
                .join(Empresa, SolicitacaoAtendimento.empresa_id == Empresa.id)
                .outerjoin(ClienteFinal, SolicitacaoAtendimento.cliente_id == ClienteFinal.id)
                .filter(SolicitacaoAtendimento.empresa_id == int(empresa_id))
                .filter(SolicitacaoAtendimento.status == STATUS_PENDENTE)
                .order_by(SolicitacaoAtendimento.criado_em.desc(), SolicitacaoAtendimento.id.desc())
            )
            registros = query.all()
    finally:
        db.close()

    solicitacoes = [
        {
            "id": item.SolicitacaoAtendimento.id,
            "empresa_id": item.SolicitacaoAtendimento.empresa_id,
            "empresa_nome": item.empresa_nome,
            "cliente_id": item.SolicitacaoAtendimento.cliente_id,
            "cliente_nome": item.cliente_nome,
            "cliente_telefone": item.cliente_telefone,
            "nome": item.SolicitacaoAtendimento.nome,
            "mensagem": item.SolicitacaoAtendimento.mensagem,
            "status": item.SolicitacaoAtendimento.status,
            "criado_em": item.SolicitacaoAtendimento.criado_em,
        }
        for item in registros
    ]

    return templates.TemplateResponse(
        request,
        "admin/solicitacoes_atendimento_list.html",
        page_context(
            request,
            title="Solicitações de Atendimento",
            empresas=empresas,
            solicitacoes=solicitacoes,
            selected_empresa_id=int(empresa_id) if empresa_id else None,
        ),
    )


@admin_app.post("/solicitacoes-atendimento/{solicitacao_id}/status")
async def solicitacao_atendimento_status_submit(request: Request, solicitacao_id: int):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    form = await request.form()
    empresa_id = request.query_params.get("empresa_id") or form.get("empresa_id")
    status = (form.get("status") or "").strip()

    if status not in {STATUS_EM_ATENDIMENTO, STATUS_FINALIZADO}:
        raise HTTPException(status_code=400, detail="status_invalido")

    db = SessionLocal()
    try:
        query = db.query(SolicitacaoAtendimento).filter_by(id=solicitacao_id)
        if empresa_id:
            query = query.filter(SolicitacaoAtendimento.empresa_id == int(empresa_id))

        solicitacao = query.first()
        if not solicitacao:
            raise HTTPException(status_code=404, detail="solicitacao_nao_encontrada")

        atualizar_status_solicitacao_atendimento(db, solicitacao, status)
    finally:
        db.close()

    mensagem = "Solicitação atualizada com sucesso."
    return RedirectResponse(
        url=f"/admin/solicitacoes-atendimento?empresa_id={empresa_id or solicitacao.empresa_id}&message={mensagem}",
        status_code=303,
    )


@admin_app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = _query_empresa_id(request)
    db = SessionLocal()
    try:
        empresas, empresa = _base_empresa_contexto(db, empresa_id)
        frases = gerar_insights(db, empresa.id if empresa else empresa_id)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/insights.html",
        page_context(
            request,
            title="Insights",
            empresas=empresas,
            selected_empresa_id=empresa.id if empresa else empresa_id,
            insights=frases,
        ),
    )


DIAS_INATIVIDADE_PERMITIDOS = (30, 60, 90, 180)


@admin_app.get("/clientes-inativos", response_class=HTMLResponse)
async def clientes_inativos_page(request: Request):
    redirect_response = admin_required(request)
    if redirect_response:
        return redirect_response

    empresa_id = _query_empresa_id(request)
    try:
        dias = int(request.query_params.get("dias") or 90)
    except ValueError:
        dias = 90
    if dias not in DIAS_INATIVIDADE_PERMITIDOS:
        dias = 90

    db = SessionLocal()
    try:
        empresas, empresa = _base_empresa_contexto(db, empresa_id)
        clientes = listar_clientes_inativos(db, empresa.id if empresa else empresa_id, dias)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin/clientes_inativos.html",
        page_context(
            request,
            title="Clientes inativos",
            empresas=empresas,
            selected_empresa_id=empresa.id if empresa else empresa_id,
            clientes=clientes,
            dias_opcoes=DIAS_INATIVIDADE_PERMITIDOS,
            selected_dias=dias,
        ),
    )