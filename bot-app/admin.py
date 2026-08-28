import hmac
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from core.config import settings
from core.database import get_db
from core.models import (
    Agendamento,
    ClienteFinal,
    ConversaIniciada,
    Empresa,
    EmpresaConhecimento,
    Servico,
    SolicitacaoAtendimento,
    UsuarioPainel,
)
from core.rate_limit import excedeu_limite, ip_do_cliente
from integrations.email_client import EmailError, email_configurado, enviar_email
from integrations.evolution_client import (
    EvolutionAPIConexaoError,
    EvolutionAPIError,
    criar_instancia,
    estado_conexao,
    excluir_instancia,
    gerar_qrcode,
    instancia_existe,
    qrcode_para_json,
)
from services.atendimento_humano import STATUS_EM_ATENDIMENTO, STATUS_FINALIZADO, STATUS_PENDENTE, atualizar_status_solicitacao_atendimento
from services.conhecimento import atualizar_conhecimento, criar_conhecimento, excluir_conhecimento, listar_conhecimento
from services.configuracoes import atualizar_configuracao, obter_configuracao
from services.metricas import calcular_metricas, gerar_insights, listar_clientes_inativos
from services.texto_utils import gerar_slug
from services.usuarios import (
    PAPEL_ADMIN,
    PAPEL_OPERADOR,
    atualizar_usuario,
    autenticar_usuario,
    criar_usuario,
    listar_usuarios,
    redefinir_senha,
    solicitar_redefinicao_senha,
    vincular_empresa,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
admin_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
SLUG_REGEX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
admin_app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax", https_only=True)


class AdminAuthRequired(Exception):
    """Levantada por `require_autenticado` quando a sessão não está autenticada."""


class EmpresaNaoVinculada(Exception):
    """Levantada por `require_admin` quando o usuário está autenticado mas ainda não tem empresa."""


@admin_app.exception_handler(AdminAuthRequired)
async def _admin_auth_required_handler(request: Request, exc: AdminAuthRequired):
    return RedirectResponse(url="/admin/login", status_code=303)


@admin_app.exception_handler(EmpresaNaoVinculada)
async def _empresa_nao_vinculada_handler(request: Request, exc: EmpresaNaoVinculada):
    return RedirectResponse(url="/admin/dashboard", status_code=303)


def require_autenticado(request: Request) -> None:
    """Só exige sessão autenticada — usado nas rotas que uma conta sem empresa ainda pode acessar."""
    if not request.session.get("admin_authenticated"):
        raise AdminAuthRequired()


def require_admin(request: Request) -> None:
    """Exige sessão autenticada E vinculada a uma empresa (superadmin sempre passa)."""
    require_autenticado(request)
    if not request.session.get("is_superadmin") and not request.session.get("usuario_empresa_id"):
        raise EmpresaNaoVinculada()


def require_superadmin(request: Request) -> None:
    """Restringe a rotas de operação da plataforma (todas as empresas, config global)."""
    require_admin(request)
    if not request.session.get("is_superadmin"):
        raise HTTPException(status_code=403, detail="acesso_restrito_ao_superadmin")


def require_papel_admin(request: Request) -> None:
    """Bloqueia o papel 'operador'; superadmin e papel 'admin' passam."""
    require_admin(request)
    if request.session.get("is_superadmin"):
        return
    if request.session.get("usuario_papel") != PAPEL_ADMIN:
        raise HTTPException(status_code=403, detail="acao_restrita_ao_papel_admin")


def page_context(request: Request, **kwargs):
    contexto = {
        "request": request,
        "admin_username": request.session.get("admin_username"),
        "is_superadmin": bool(request.session.get("is_superadmin")),
        "usuario_papel": request.session.get("usuario_papel"),
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


def _normalizar_telefone(telefone: str | None) -> str:
    return re.sub(r"\D", "", telefone or "")


def _sessao_empresa_id(request: Request) -> int | None:
    """Empresa à qual a sessão está restrita, ou None para superadmin (acesso a todas)."""
    return request.session.get("usuario_empresa_id")


def _query_empresa_id(request: Request):
    """Empresa efetiva da requisição: forçada pela sessão quando escopada, senão o filtro da query string."""
    escopo = _sessao_empresa_id(request)
    if escopo is not None:
        return escopo
    valor = request.query_params.get("empresa_id")
    if valor in {None, ""}:
        return None
    return int(valor)


def _empresa_id_do_formulario(request: Request, form) -> int:
    """Como `_query_empresa_id`, mas para o valor submetido em formulários de criação/edição."""
    escopo = _sessao_empresa_id(request)
    if escopo is not None:
        return escopo
    return int(form.get("empresa_id"))


def _empresas_visiveis(db, request: Request):
    """Lista de empresas para seletores/dropdowns: só a própria quando a sessão é escopada."""
    escopo = _sessao_empresa_id(request)
    if escopo is not None:
        empresa = db.query(Empresa).filter_by(id=escopo).first()
        return [empresa] if empresa else []
    return db.query(Empresa).order_by(Empresa.nome.asc()).all()


def _base_empresa_contexto(db, request: Request, empresa_id: int | None):
    escopo = _sessao_empresa_id(request)
    if escopo is not None:
        empresa = db.query(Empresa).filter_by(id=escopo).first()
        return ([empresa] if empresa else []), empresa

    empresas = db.query(Empresa).order_by(Empresa.nome.asc()).all()
    empresa = db.query(Empresa).filter_by(id=empresa_id).first() if empresa_id else None
    if empresa is None and len(empresas) == 1:
        empresa = empresas[0]
    return empresas, empresa


def _assert_acesso_empresa(request: Request, empresa_id_recurso: int | None) -> None:
    """Levanta 404 se o recurso pertence a outra empresa que não a da sessão escopada."""
    escopo = _sessao_empresa_id(request)
    if escopo is not None and empresa_id_recurso != escopo:
        raise HTTPException(status_code=404, detail="recurso_nao_encontrado")


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


def load_empresa(db, request: Request, empresa_id: int):
    empresa = db.query(Empresa).filter(Empresa.id == empresa_id).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="empresa_nao_encontrada")
    _assert_acesso_empresa(request, empresa.id)
    return empresa


def load_servico(db, request: Request, servico_id: int):
    servico = db.query(Servico).filter(Servico.id == servico_id).first()
    if not servico:
        raise HTTPException(status_code=404, detail="servico_nao_encontrado")
    _assert_acesso_empresa(request, servico.empresa_id)
    return servico


def load_cliente(db, request: Request, cliente_id: int):
    cliente = db.query(ClienteFinal).filter(ClienteFinal.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="cliente_nao_encontrado")
    _assert_acesso_empresa(request, cliente.empresa_id)
    return cliente


def load_agendamento(db, request: Request, agendamento_id: int):
    agendamento = db.query(Agendamento).filter(Agendamento.id == agendamento_id).first()
    if not agendamento:
        raise HTTPException(status_code=404, detail="agendamento_nao_encontrado")
    _assert_acesso_empresa(request, agendamento.empresa_id)
    return agendamento


def load_conhecimento(db, request: Request, conhecimento_id: int):
    entrada = db.query(EmpresaConhecimento).filter(EmpresaConhecimento.id == conhecimento_id).first()
    if not entrada:
        raise HTTPException(status_code=404, detail="conhecimento_nao_encontrado")
    _assert_acesso_empresa(request, entrada.empresa_id)
    return entrada


def load_solicitacao(db, request: Request, solicitacao_id: int):
    solicitacao = db.query(SolicitacaoAtendimento).filter(SolicitacaoAtendimento.id == solicitacao_id).first()
    if not solicitacao:
        raise HTTPException(status_code=404, detail="solicitacao_nao_encontrada")
    _assert_acesso_empresa(request, solicitacao.empresa_id)
    return solicitacao


def load_usuario(db, request: Request, usuario_id: int):
    usuario = db.query(UsuarioPainel).filter(UsuarioPainel.id == usuario_id).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="usuario_nao_encontrado")
    _assert_acesso_empresa(request, usuario.empresa_id)
    return usuario


def _aplicar_dados_empresa(empresa: Empresa, dados: SimpleNamespace):
    empresa.nome = dados.nome
    empresa.slug = dados.slug
    empresa.segmento = dados.segmento
    empresa.telefone_whatsapp = dados.telefone_whatsapp
    empresa.email = dados.email
    empresa.endereco = dados.endereco
    empresa.descricao = dados.descricao
    empresa.logo_url = dados.logo_url
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


def _excluir_empresa_em_cascata(db, empresa: Empresa) -> None:
    """Apaga a empresa e todos os dados vinculados a ela — exclusão definitiva, sem volta.

    Existe pra permitir testar o produto de ponta a ponta (cadastrar, usar,
    descadastrar) sem acumular lixo no banco — "desativar" (`Empresa.ativo`)
    não serve pra isso, porque mantém os dados. Ordem de exclusão respeita as
    FKs: agendamentos e solicitações primeiro (referenciam cliente/serviço),
    só depois clientes e serviços.

    Usuários do painel vinculados a essa empresa não são apagados — ficam sem
    empresa (mesmo estado de quem ainda não cadastrou nenhuma), podem vincular
    outra depois em `/admin/empresas/cadastrar`.
    """
    empresa_id = empresa.id

    db.query(Agendamento).filter(Agendamento.empresa_id == empresa_id).delete(synchronize_session=False)
    db.query(SolicitacaoAtendimento).filter(SolicitacaoAtendimento.empresa_id == empresa_id).delete(synchronize_session=False)
    db.query(ClienteFinal).filter(ClienteFinal.empresa_id == empresa_id).delete(synchronize_session=False)
    db.query(Servico).filter(Servico.empresa_id == empresa_id).delete(synchronize_session=False)
    db.query(EmpresaConhecimento).filter(EmpresaConhecimento.empresa_id == empresa_id).delete(synchronize_session=False)
    db.query(ConversaIniciada).filter(ConversaIniciada.empresa_id == empresa_id).delete(synchronize_session=False)
    db.query(UsuarioPainel).filter(UsuarioPainel.empresa_id == empresa_id).update(
        {UsuarioPainel.empresa_id: None}, synchronize_session=False
    )
    db.delete(empresa)
    db.commit()


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
async def login_submit(request: Request, db: Session = Depends(get_db)):
    if excedeu_limite(f"ratelimit:login:{ip_do_cliente(request)}", limite=10, janela_segundos=60):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            page_context(request, title="Entrar", error="Muitas tentativas. Aguarde um minuto e tente novamente."),
            status_code=429,
        )

    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""

    superadmin_valido = hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )
    if superadmin_valido:
        request.session["admin_authenticated"] = True
        request.session["admin_username"] = username
        request.session["is_superadmin"] = True
        request.session["usuario_id"] = None
        request.session["usuario_empresa_id"] = None
        request.session["usuario_papel"] = None
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    usuario = autenticar_usuario(db, username, password)
    if usuario:
        request.session["admin_authenticated"] = True
        request.session["admin_username"] = usuario.nome or usuario.email
        request.session["is_superadmin"] = False
        request.session["usuario_id"] = usuario.id
        request.session["usuario_empresa_id"] = usuario.empresa_id
        request.session["usuario_papel"] = usuario.papel
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    return templates.TemplateResponse(
        request,
        "admin/login.html",
        page_context(request, title="Entrar", error="Usuário ou senha inválidos."),
        status_code=401,
    )


@admin_app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@admin_app.get("/esqueci-senha", response_class=HTMLResponse)
async def esqueci_senha_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/esqueci_senha.html",
        page_context(request, title="Recuperar acesso"),
    )


@admin_app.post("/esqueci-senha")
async def esqueci_senha_submit(request: Request, db: Session = Depends(get_db)):
    if excedeu_limite(f"ratelimit:esqueci-senha:{ip_do_cliente(request)}", limite=5, janela_segundos=60):
        return templates.TemplateResponse(
            request,
            "admin/esqueci_senha.html",
            page_context(request, title="Recuperar acesso", error="Muitas tentativas. Aguarde um minuto e tente novamente."),
            status_code=429,
        )

    form = await request.form()
    email = (form.get("email") or "").strip().lower()

    token = solicitar_redefinicao_senha(db, email) if email else None
    if token:
        link = f"{settings.public_base_url}/admin/redefinir-senha?token={token}"
        corpo = (
            "Recebemos um pedido para redefinir a senha do seu acesso ao painel.\n\n"
            f"Se foi você, clique no link abaixo (válido por 1 hora):\n{link}\n\n"
            "Se não foi você, pode ignorar este e-mail."
        )
        try:
            await enviar_email(email, "Redefinição de senha - WhatsApp SaaS", corpo)
        except EmailError:
            logger.exception("Falha ao enviar e-mail de redefinição de senha para %s", email)

    return templates.TemplateResponse(
        request,
        "admin/esqueci_senha.html",
        page_context(
            request,
            title="Recuperar acesso",
            message="Se esse e-mail estiver cadastrado, enviamos instruções para redefinir a senha.",
        ),
    )


@admin_app.get("/redefinir-senha", response_class=HTMLResponse)
async def redefinir_senha_page(request: Request):
    token = request.query_params.get("token") or ""
    return templates.TemplateResponse(
        request,
        "admin/redefinir_senha.html",
        page_context(request, title="Redefinir senha", token=token),
    )


@admin_app.post("/redefinir-senha")
async def redefinir_senha_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    token = (form.get("token") or "").strip()
    nova_senha = form.get("senha") or ""

    erro = None
    if len(nova_senha) < 8:
        erro = "A senha deve ter pelo menos 8 caracteres."
    else:
        usuario = redefinir_senha(db, token, nova_senha)
        if not usuario:
            erro = "Link inválido ou expirado. Solicite uma nova redefinição."

    if erro:
        return templates.TemplateResponse(
            request,
            "admin/redefinir_senha.html",
            page_context(request, title="Redefinir senha", token=token, error=erro),
            status_code=400,
        )

    return RedirectResponse(
        url="/admin/login?message=Senha redefinida com sucesso. Entre com a nova senha.",
        status_code=303,
    )


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
async def dashboard(request: Request, db: Session = Depends(get_db), _: None = Depends(require_autenticado)):
    if not request.session.get("is_superadmin") and not request.session.get("usuario_empresa_id"):
        return templates.TemplateResponse(
            request,
            "admin/dashboard_sem_empresa.html",
            page_context(request, title="Bem-vindo"),
        )

    empresa_id = _query_empresa_id(request)
    data_inicio, data_fim = _periodo_dashboard(request)

    empresas, empresa = _base_empresa_contexto(db, request, empresa_id)
    metricas_periodo = calcular_metricas(db, empresa_id, data_inicio, data_fim)

    status_bot = None
    if empresa and not empresa.ativo:
        status_bot = await _status_configuracao_bot(db, empresa)

    total_empresas = None
    empresas_ativas = None
    if request.session.get("is_superadmin"):
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
            empresa=empresa,
            status_bot=status_bot,
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
async def empresas_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_superadmin)):
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
async def empresa_new_page(request: Request, _: None = Depends(require_superadmin)):
    return templates.TemplateResponse(
        request,
        "admin/empresa_form.html",
        page_context(request, title="Nova empresa", empresa=None, action_url="/admin/empresas/nova"),
    )


@admin_app.post("/empresas/nova")
async def empresa_new_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_superadmin)):
    form = await request.form()
    dados = form_namespace(form)

    erro_validacao = None
    if not dados.slug or not SLUG_REGEX.fullmatch(dados.slug):
        sugestao = gerar_slug(dados.nome or dados.slug or "")
        dica = f" Sugestão: {sugestao}" if sugestao else ""
        erro_validacao = f"Use um slug com apenas letras minúsculas, números e hífens (sem acentos ou espaços).{dica}"
    elif db.query(Empresa.id).filter(Empresa.slug == dados.slug).first():
        erro_validacao = "Já existe uma empresa com esse slug."
    elif not _normalizar_telefone(dados.telefone_whatsapp):
        erro_validacao = "Informe o telefone WhatsApp para conectar a empresa."

    if erro_validacao:
        return templates.TemplateResponse(
            request,
            "admin/empresa_form.html",
            page_context(request, title="Nova empresa", empresa=dados, action_url="/admin/empresas/nova", error=erro_validacao),
            status_code=400,
        )

    nome_instancia = dados.slug
    telefone_normalizado = _normalizar_telefone(dados.telefone_whatsapp)
    webhook_url = f"{settings.public_base_url}/webhook?token={quote(settings.webhook_secret)}"

    if await instancia_existe(nome_instancia):
        return templates.TemplateResponse(
            request,
            "admin/empresa_form.html",
            page_context(
                request,
                title="Nova empresa",
                empresa=dados,
                action_url="/admin/empresas/nova",
                error=f"Já existe uma instância de WhatsApp com o identificador '{nome_instancia}'. Escolha outro slug.",
            ),
            status_code=400,
        )

    try:
        await criar_instancia(nome_instancia, telefone_normalizado, webhook_url)
    except EvolutionAPIConexaoError:
        return templates.TemplateResponse(
            request,
            "admin/empresa_form.html",
            page_context(
                request,
                title="Nova empresa",
                empresa=dados,
                action_url="/admin/empresas/nova",
                error="Não foi possível conectar ao serviço de WhatsApp agora. Tente novamente em alguns instantes.",
            ),
            status_code=502,
        )
    except EvolutionAPIError as exc:
        return templates.TemplateResponse(
            request,
            "admin/empresa_form.html",
            page_context(
                request,
                title="Nova empresa",
                empresa=dados,
                action_url="/admin/empresas/nova",
                error=f"O WhatsApp recusou a conexão: {exc}. Verifique o número informado e tente novamente.",
            ),
            status_code=502,
        )

    try:
        empresa = Empresa()
        _aplicar_dados_empresa(empresa, dados)
        empresa.telefone_whatsapp = telefone_normalizado
        empresa.evolution_instance_name = nome_instancia
        db.add(empresa)
        db.commit()
        db.refresh(empresa)
    except IntegrityError:
        db.rollback()
        logger.exception("Conflito ao salvar nova empresa pelo painel (slug=%s)", dados.slug)
        await excluir_instancia(nome_instancia)
        return templates.TemplateResponse(
            request,
            "admin/empresa_form.html",
            page_context(
                request,
                title="Nova empresa",
                empresa=dados,
                action_url="/admin/empresas/nova",
                error="Esse slug acabou de ser usado por outro cadastro. Escolha outro.",
            ),
            status_code=400,
        )

    return RedirectResponse(
        url=f"/admin/empresas/{empresa.id}/conectar?message=Empresa criada com sucesso. Escaneie o QR code para conectar o WhatsApp.",
        status_code=303,
    )


@admin_app.get("/empresas/cadastrar", response_class=HTMLResponse)
async def empresa_cadastrar_page(request: Request, _: None = Depends(require_autenticado)):
    if request.session.get("is_superadmin") or request.session.get("usuario_empresa_id"):
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    return templates.TemplateResponse(
        request,
        "admin/empresa_cadastrar.html",
        page_context(request, title="Cadastrar minha empresa", empresa=None, action_url="/admin/empresas/cadastrar"),
    )


@admin_app.post("/empresas/cadastrar")
async def empresa_cadastrar_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_autenticado)):
    if request.session.get("is_superadmin") or request.session.get("usuario_empresa_id"):
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    usuario = db.query(UsuarioPainel).filter_by(id=request.session.get("usuario_id")).first()
    if not usuario:
        request.session.clear()
        return RedirectResponse(url="/admin/login", status_code=303)

    form = await request.form()
    dados = form_namespace(form)

    erro_validacao = None
    if not dados.slug or not SLUG_REGEX.fullmatch(dados.slug):
        sugestao = gerar_slug(dados.nome or dados.slug or "")
        dica = f" Sugestão: {sugestao}" if sugestao else ""
        erro_validacao = f"Use um slug com apenas letras minúsculas, números e hífens (sem acentos ou espaços).{dica}"
    elif db.query(Empresa.id).filter(Empresa.slug == dados.slug).first():
        erro_validacao = "Já existe uma empresa com esse slug."
    elif not _normalizar_telefone(dados.telefone_whatsapp):
        erro_validacao = "Informe o telefone WhatsApp para conectar a empresa."

    if erro_validacao:
        return templates.TemplateResponse(
            request,
            "admin/empresa_cadastrar.html",
            page_context(request, title="Cadastrar minha empresa", empresa=dados, action_url="/admin/empresas/cadastrar", error=erro_validacao),
            status_code=400,
        )

    nome_instancia = dados.slug
    telefone_normalizado = _normalizar_telefone(dados.telefone_whatsapp)
    webhook_url = f"{settings.public_base_url}/webhook?token={quote(settings.webhook_secret)}"

    if await instancia_existe(nome_instancia):
        return templates.TemplateResponse(
            request,
            "admin/empresa_cadastrar.html",
            page_context(
                request,
                title="Cadastrar minha empresa",
                empresa=dados,
                action_url="/admin/empresas/cadastrar",
                error=f"Já existe uma instância de WhatsApp com o identificador '{nome_instancia}'. Escolha outro slug.",
            ),
            status_code=400,
        )

    try:
        await criar_instancia(nome_instancia, telefone_normalizado, webhook_url)
    except EvolutionAPIConexaoError:
        return templates.TemplateResponse(
            request,
            "admin/empresa_cadastrar.html",
            page_context(
                request,
                title="Cadastrar minha empresa",
                empresa=dados,
                action_url="/admin/empresas/cadastrar",
                error="Não foi possível conectar ao serviço de WhatsApp agora. Tente novamente em alguns instantes.",
            ),
            status_code=502,
        )
    except EvolutionAPIError as exc:
        return templates.TemplateResponse(
            request,
            "admin/empresa_cadastrar.html",
            page_context(
                request,
                title="Cadastrar minha empresa",
                empresa=dados,
                action_url="/admin/empresas/cadastrar",
                error=f"O WhatsApp recusou a conexão: {exc}. Verifique o número informado e tente novamente.",
            ),
            status_code=502,
        )

    try:
        empresa = Empresa()
        _aplicar_dados_empresa(empresa, dados)
        empresa.telefone_whatsapp = telefone_normalizado
        empresa.evolution_instance_name = nome_instancia
        empresa.ativo = False
        db.add(empresa)
        db.commit()
        db.refresh(empresa)

        vincular_empresa(db, usuario, empresa.id)
    except IntegrityError:
        db.rollback()
        logger.exception("Conflito ao salvar empresa self-service (slug=%s)", dados.slug)
        await excluir_instancia(nome_instancia)
        return templates.TemplateResponse(
            request,
            "admin/empresa_cadastrar.html",
            page_context(
                request,
                title="Cadastrar minha empresa",
                empresa=dados,
                action_url="/admin/empresas/cadastrar",
                error="Esse slug acabou de ser usado por outro cadastro. Escolha outro.",
            ),
            status_code=400,
        )

    request.session["usuario_empresa_id"] = usuario.empresa_id
    request.session["usuario_papel"] = usuario.papel

    return RedirectResponse(
        url=f"/admin/empresas/{empresa.id}/conectar?message=Empresa criada com sucesso. Escaneie o QR code para conectar o WhatsApp.",
        status_code=303,
    )


@admin_app.get("/empresas/{empresa_id}/editar", response_class=HTMLResponse)
async def empresa_edit_page(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)

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
async def empresa_edit_submit(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    form = await request.form()
    try:
        empresa = load_empresa(db, request, empresa_id)
        _aplicar_dados_empresa(empresa, form_namespace(form, empresa_id=empresa_id))
        db.commit()
    except IntegrityError:
        db.rollback()
        empresa = load_empresa(db, request, empresa_id)
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

    return RedirectResponse(url="/admin/empresas?message=Empresa atualizada com sucesso.", status_code=303)


@admin_app.post("/empresas/{empresa_id}/toggle")
async def empresa_toggle(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_superadmin)):
    empresa = load_empresa(db, request, empresa_id)
    empresa.ativo = not empresa.ativo
    if empresa.ativo and not empresa.ativado_em:
        empresa.ativado_em = datetime.utcnow()
    db.commit()

    return RedirectResponse(url="/admin/empresas?message=Status da empresa atualizado.", status_code=303)


@admin_app.post("/empresas/{empresa_id}/ativar")
async def empresa_ativar(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)
    empresa.ativo = True
    if not empresa.ativado_em:
        empresa.ativado_em = datetime.utcnow()
    db.commit()

    return RedirectResponse(url="/admin/configurar-bot?message=Bot ativado. Seus clientes já podem agendar pelo WhatsApp.", status_code=303)


@admin_app.post("/empresas/{empresa_id}/pausar")
async def empresa_pausar(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)
    empresa.ativo = False
    db.commit()

    return RedirectResponse(url="/admin/configurar-bot?message=Bot pausado. Ele não vai responder no WhatsApp até você ativar de novo.", status_code=303)


@admin_app.get("/empresas/{empresa_id}/excluir", response_class=HTMLResponse)
async def empresa_excluir_page(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)
    contagens = {
        "servicos": db.query(func.count(Servico.id)).filter(Servico.empresa_id == empresa_id).scalar() or 0,
        "clientes": db.query(func.count(ClienteFinal.id)).filter(ClienteFinal.empresa_id == empresa_id).scalar() or 0,
        "agendamentos": db.query(func.count(Agendamento.id)).filter(Agendamento.empresa_id == empresa_id).scalar() or 0,
        "usuarios": db.query(func.count(UsuarioPainel.id)).filter(UsuarioPainel.empresa_id == empresa_id).scalar() or 0,
    }

    return templates.TemplateResponse(
        request,
        "admin/empresa_excluir.html",
        page_context(request, title="Excluir empresa", empresa=empresa, contagens=contagens),
    )


@admin_app.post("/empresas/{empresa_id}/excluir")
async def empresa_excluir_submit(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)
    nome_instancia = empresa.evolution_instance_name
    excluindo_a_propria_empresa = request.session.get("usuario_empresa_id") == empresa_id

    _excluir_empresa_em_cascata(db, empresa)

    if nome_instancia:
        await excluir_instancia(nome_instancia)

    if excluindo_a_propria_empresa:
        request.session["usuario_empresa_id"] = None
        request.session["usuario_papel"] = None
        return RedirectResponse(url="/admin/dashboard?message=Empresa excluída com sucesso.", status_code=303)

    return RedirectResponse(url="/admin/empresas?message=Empresa excluída com sucesso.", status_code=303)


@admin_app.get("/empresas/{empresa_id}/conectar", response_class=HTMLResponse)
async def empresa_conectar_page(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)

    qrcode_erro = None
    try:
        qrcode = await gerar_qrcode(empresa.evolution_instance_name, empresa.telefone_whatsapp)
    except EvolutionAPIError:
        qrcode = None
        qrcode_erro = "Não foi possível gerar o código de conexão agora. Tente novamente."

    return templates.TemplateResponse(
        request,
        "admin/empresa_conectar.html",
        page_context(
            request,
            title=f"Conectar WhatsApp - {empresa.nome}",
            empresa=empresa,
            qrcode=qrcode,
            qrcode_json=qrcode_para_json(qrcode),
            qrcode_erro=qrcode_erro,
            status_url=f"/admin/empresas/{empresa_id}/conectar/status",
            refresh_url=f"/admin/empresas/{empresa_id}/conectar/novo-qrcode",
            next_url="/admin/configurar-bot?message=WhatsApp conectado! Agora vamos configurar como seu bot deve atender os clientes.",
        ),
    )


@admin_app.get("/empresas/{empresa_id}/conectar/status")
async def empresa_conectar_status(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)
    try:
        state = await estado_conexao(empresa.evolution_instance_name)
    except EvolutionAPIError:
        state = "close"
    return {"state": state}


@admin_app.post("/empresas/{empresa_id}/conectar/novo-qrcode")
async def empresa_conectar_novo_qrcode(request: Request, empresa_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa = load_empresa(db, request, empresa_id)
    try:
        qrcode = await gerar_qrcode(empresa.evolution_instance_name, empresa.telefone_whatsapp)
    except EvolutionAPIError:
        raise HTTPException(status_code=502, detail="falha_ao_gerar_qrcode")
    return qrcode


async def _status_configuracao_bot(db, empresa: Empresa) -> SimpleNamespace:
    """Checklist de configuração calculado ao vivo — reaproveitado pelo hub e pelo dashboard."""
    whatsapp_conectado = False
    whatsapp_erro = False
    if empresa.evolution_instance_name:
        try:
            whatsapp_conectado = await estado_conexao(empresa.evolution_instance_name) == "open"
        except EvolutionAPIError:
            whatsapp_erro = True

    total_servicos_ativos = (
        db.query(func.count(Servico.id))
        .filter_by(empresa_id=empresa.id, ativo=True)
        .filter(Servico.excluido_em.is_(None))
        .scalar()
        or 0
    )
    atendimento_personalizado = bool(empresa.mensagem_boas_vindas or empresa.mensagem_fora_horario or empresa.mensagem_atendimento_humano or empresa.mensagem_encerramento)
    pronto_para_ativar = whatsapp_conectado and total_servicos_ativos > 0

    return SimpleNamespace(
        whatsapp_conectado=whatsapp_conectado,
        whatsapp_erro=whatsapp_erro,
        total_servicos_ativos=total_servicos_ativos,
        atendimento_personalizado=atendimento_personalizado,
        pronto_para_ativar=pronto_para_ativar,
    )


@admin_app.get("/configurar-bot", response_class=HTMLResponse)
async def configurar_bot(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    _, empresa = _base_empresa_contexto(db, request, empresa_id)
    if not empresa:
        return RedirectResponse(url="/admin/empresas", status_code=303)

    status = await _status_configuracao_bot(db, empresa)

    return templates.TemplateResponse(
        request,
        "admin/configurar_bot.html",
        page_context(
            request,
            title="Configurar meu bot",
            empresa=empresa,
            whatsapp_conectado=status.whatsapp_conectado,
            whatsapp_erro=status.whatsapp_erro,
            total_servicos_ativos=status.total_servicos_ativos,
            atendimento_personalizado=status.atendimento_personalizado,
            pronto_para_ativar=status.pronto_para_ativar,
        ),
    )


@admin_app.get("/configurar-bot/atendimento", response_class=HTMLResponse)
async def configurar_bot_atendimento_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    _, empresa = _base_empresa_contexto(db, request, empresa_id)
    if not empresa:
        return RedirectResponse(url="/admin/configurar-bot", status_code=303)

    return templates.TemplateResponse(
        request,
        "admin/configurar_bot_atendimento.html",
        page_context(request, title="Personalize o atendimento", empresa=empresa),
    )


@admin_app.post("/configurar-bot/atendimento")
async def configurar_bot_atendimento_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    _, empresa = _base_empresa_contexto(db, request, empresa_id)
    if not empresa:
        return RedirectResponse(url="/admin/configurar-bot", status_code=303)

    form = await request.form()
    empresa.mensagem_boas_vindas = parse_optional_str(form.get("mensagem_boas_vindas"))
    empresa.mensagem_fora_horario = parse_optional_str(form.get("mensagem_fora_horario"))
    empresa.mensagem_atendimento_humano = parse_optional_str(form.get("mensagem_atendimento_humano"))
    empresa.mensagem_encerramento = parse_optional_str(form.get("mensagem_encerramento"))
    db.commit()

    return RedirectResponse(url="/admin/configurar-bot?message=Atendimento personalizado com sucesso.", status_code=303)


@admin_app.get("/configurar-bot/lembretes", response_class=HTMLResponse)
async def configurar_bot_lembretes_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    _, empresa = _base_empresa_contexto(db, request, empresa_id)
    if not empresa:
        return RedirectResponse(url="/admin/configurar-bot", status_code=303)

    config = obter_configuracao(db)

    return templates.TemplateResponse(
        request,
        "admin/configurar_bot_lembretes.html",
        page_context(
            request,
            title="Lembretes automáticos",
            empresa=empresa,
            email_configurado=email_configurado(config),
        ),
    )


@admin_app.post("/configurar-bot/lembretes")
async def configurar_bot_lembretes_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    _, empresa = _base_empresa_contexto(db, request, empresa_id)
    if not empresa:
        return RedirectResponse(url="/admin/configurar-bot", status_code=303)

    form = await request.form()
    empresa.lembrete_canal_email = parse_bool(form.get("lembrete_canal_email"))
    db.commit()

    return RedirectResponse(url="/admin/configurar-bot?message=Canais de lembrete atualizados com sucesso.", status_code=303)


@admin_app.get("/configurar-bot/horarios", response_class=HTMLResponse)
async def configurar_bot_horarios_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    _, empresa = _base_empresa_contexto(db, request, empresa_id)
    if not empresa:
        return RedirectResponse(url="/admin/configurar-bot", status_code=303)

    return templates.TemplateResponse(
        request,
        "admin/configurar_bot_horarios.html",
        page_context(request, title="Configure seus horários", empresa=empresa),
    )


@admin_app.post("/configurar-bot/horarios")
async def configurar_bot_horarios_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    _, empresa = _base_empresa_contexto(db, request, empresa_id)
    if not empresa:
        return RedirectResponse(url="/admin/configurar-bot", status_code=303)

    form = await request.form()
    empresa.horario_abertura = (form.get("horario_abertura") or "08:00").strip()
    empresa.horario_fechamento = (form.get("horario_fechamento") or "18:00").strip()
    empresa.intervalo_entre_atendimentos_minutos = parse_optional_int(form.get("intervalo_entre_atendimentos_minutos")) or 15
    empresa.dias_indisponiveis = (form.get("dias_indisponiveis") or "").strip()
    empresa.datas_indisponiveis = (form.get("datas_indisponiveis") or "").strip()
    db.commit()

    return RedirectResponse(url="/admin/configurar-bot?message=Horários atualizados com sucesso.", status_code=303)


@admin_app.get("/servicos", response_class=HTMLResponse)
async def servicos_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    empresas = _empresas_visiveis(db, request)
    query = (
        db.query(Servico)
        .options(joinedload(Servico.empresa))
        .filter(Servico.excluido_em.is_(None))
        .order_by(Servico.ordem_exibicao.asc(), Servico.nome.asc())
    )
    if empresa_id:
        query = query.filter(Servico.empresa_id == empresa_id)
    servicos = query.all()

    return templates.TemplateResponse(
        request,
        "admin/servicos_list.html",
        page_context(
            request,
            title="Serviços",
            empresas=empresas,
            servicos=servicos,
            selected_empresa_id=empresa_id,
        ),
    )


@admin_app.get("/servicos/novo", response_class=HTMLResponse)
async def servico_new_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    empresas = _empresas_visiveis(db, request)

    return templates.TemplateResponse(
        request,
        "admin/servico_form.html",
        page_context(
            request,
            title="Novo serviço",
            empresas=empresas,
            servico=None,
            selected_empresa_id=empresa_id,
            action_url="/admin/servicos/novo",
        ),
    )


@admin_app.post("/servicos/novo")
async def servico_new_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    form = await request.form()
    empresa_id = _empresa_id_do_formulario(request, form)
    load_empresa(db, request, empresa_id)
    dados = form_namespace(form, empresa_id=empresa_id)
    servico = Servico()
    _aplicar_dados_servico(servico, dados)
    db.add(servico)
    db.commit()

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Serviço criado com sucesso.", status_code=303)


@admin_app.get("/servicos/{servico_id}/editar", response_class=HTMLResponse)
async def servico_edit_page(request: Request, servico_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresas = _empresas_visiveis(db, request)
    servico = load_servico(db, request, servico_id)

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
async def servico_edit_submit(request: Request, servico_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    form = await request.form()
    servico = load_servico(db, request, servico_id)
    empresa_id = _empresa_id_do_formulario(request, form)
    load_empresa(db, request, empresa_id)
    _aplicar_dados_servico(servico, form_namespace(form, empresa_id=empresa_id))
    db.commit()

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Serviço atualizado com sucesso.", status_code=303)


@admin_app.post("/servicos/{servico_id}/toggle")
async def servico_toggle(request: Request, servico_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    servico = load_servico(db, request, servico_id)
    servico.ativo = not servico.ativo
    db.commit()
    empresa_id = servico.empresa_id

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Status do serviço atualizado.", status_code=303)


@admin_app.post("/servicos/{servico_id}/excluir")
async def servico_delete(request: Request, servico_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    servico = load_servico(db, request, servico_id)
    servico.ativo = False
    servico.excluido_em = datetime.utcnow()
    empresa_id = servico.empresa_id
    db.commit()

    return RedirectResponse(url=f"/admin/servicos?empresa_id={empresa_id}&message=Serviço excluído com sucesso.", status_code=303)


@admin_app.get("/conhecimento", response_class=HTMLResponse)
async def conhecimento_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    empresas = _empresas_visiveis(db, request)
    entradas = listar_conhecimento(db, empresa_id)

    return templates.TemplateResponse(
        request,
        "admin/conhecimento_list.html",
        page_context(
            request,
            title="Base de conhecimento",
            empresas=empresas,
            entradas=entradas,
            selected_empresa_id=empresa_id,
        ),
    )


@admin_app.get("/conhecimento/novo", response_class=HTMLResponse)
async def conhecimento_new_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    empresas = _empresas_visiveis(db, request)

    return templates.TemplateResponse(
        request,
        "admin/conhecimento_form.html",
        page_context(
            request,
            title="Nova pergunta",
            empresas=empresas,
            entrada=None,
            selected_empresa_id=empresa_id,
            action_url="/admin/conhecimento/novo",
        ),
    )


@admin_app.post("/conhecimento/novo")
async def conhecimento_new_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    form = await request.form()
    empresa_id = _empresa_id_do_formulario(request, form)
    load_empresa(db, request, empresa_id)
    criar_conhecimento(
        db,
        empresa_id=empresa_id,
        categoria=parse_optional_str(form.get("categoria")),
        pergunta=(form.get("pergunta") or "").strip(),
        resposta=(form.get("resposta") or "").strip(),
        ativo=parse_bool(form.get("ativo")),
    )

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Pergunta criada com sucesso.", status_code=303)


@admin_app.get("/conhecimento/{conhecimento_id}/editar", response_class=HTMLResponse)
async def conhecimento_edit_page(request: Request, conhecimento_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresas = _empresas_visiveis(db, request)
    entrada = load_conhecimento(db, request, conhecimento_id)

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
async def conhecimento_edit_submit(request: Request, conhecimento_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    form = await request.form()
    entrada = load_conhecimento(db, request, conhecimento_id)
    empresa_id = _empresa_id_do_formulario(request, form)
    load_empresa(db, request, empresa_id)
    atualizar_conhecimento(
        entrada,
        categoria=parse_optional_str(form.get("categoria")),
        pergunta=(form.get("pergunta") or "").strip(),
        resposta=(form.get("resposta") or "").strip(),
        ativo=parse_bool(form.get("ativo")),
    )
    db.commit()

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Pergunta atualizada com sucesso.", status_code=303)


@admin_app.post("/conhecimento/{conhecimento_id}/toggle")
async def conhecimento_toggle(request: Request, conhecimento_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    entrada = load_conhecimento(db, request, conhecimento_id)
    entrada.ativo = not entrada.ativo
    db.commit()
    empresa_id = entrada.empresa_id

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Status atualizado.", status_code=303)


@admin_app.post("/conhecimento/{conhecimento_id}/excluir")
async def conhecimento_delete(request: Request, conhecimento_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    entrada = load_conhecimento(db, request, conhecimento_id)
    excluir_conhecimento(entrada)
    empresa_id = entrada.empresa_id
    db.commit()

    return RedirectResponse(url=f"/admin/conhecimento?empresa_id={empresa_id}&message=Pergunta excluída com sucesso.", status_code=303)


@admin_app.get("/agendamentos", response_class=HTMLResponse)
async def agendamentos_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    status = request.query_params.get("status")
    data = (request.query_params.get("data") or "").strip()
    servico_id = request.query_params.get("servico_id")
    cliente_id = request.query_params.get("cliente_id")

    empresas, empresa = _base_empresa_contexto(db, request, empresa_id)
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
async def agendamento_status_submit(request: Request, agendamento_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    form = await request.form()
    status = (form.get("status") or "").strip()
    if not _agendamento_status_permitido(status):
        raise HTTPException(status_code=400, detail="status_invalido")

    agendamento = load_agendamento(db, request, agendamento_id)

    agendamento.status = status
    if status == "cancelado":
        agendamento.cancelado_em = datetime.utcnow()
    db.commit()
    empresa_id = agendamento.empresa_id

    return RedirectResponse(url=f"/admin/agendamentos?empresa_id={empresa_id}&message=Agendamento atualizado com sucesso.", status_code=303)


@admin_app.get("/clientes", response_class=HTMLResponse)
async def clientes_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    q = (request.query_params.get("q") or "").strip()
    sort = (request.query_params.get("sort") or "recente").strip()

    empresas, empresa = _base_empresa_contexto(db, request, empresa_id)
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


@admin_app.get("/clientes/novo", response_class=HTMLResponse)
async def cliente_new_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    empresas, empresa = _base_empresa_contexto(db, request, empresa_id)

    return templates.TemplateResponse(
        request,
        "admin/cliente_form.html",
        page_context(
            request,
            title="Novo cliente",
            empresas=empresas,
            cliente=None,
            selected_empresa_id=empresa.id if empresa else empresa_id,
            action_url="/admin/clientes/novo",
        ),
    )


@admin_app.post("/clientes/novo")
async def cliente_new_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    form = await request.form()
    empresa_id = _empresa_id_do_formulario(request, form)
    empresa = load_empresa(db, request, empresa_id)
    nome = parse_optional_str(form.get("nome"))
    telefone = _normalizar_telefone(form.get("telefone"))
    email = parse_optional_str(form.get("email"))

    erro = None
    if not telefone:
        erro = "Informe um telefone válido."
    elif db.query(ClienteFinal).filter_by(empresa_id=empresa.id, telefone=telefone).first():
        erro = "Já existe um cliente com esse telefone para essa empresa."

    if erro:
        empresas, _ = _base_empresa_contexto(db, request, empresa_id)
        return templates.TemplateResponse(
            request,
            "admin/cliente_form.html",
            page_context(
                request,
                title="Novo cliente",
                empresas=empresas,
                cliente=SimpleNamespace(nome=nome, telefone=form.get("telefone") or "", email=email or ""),
                selected_empresa_id=empresa_id,
                action_url="/admin/clientes/novo",
                error=erro,
            ),
            status_code=400,
        )

    cliente = ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome, email=email)
    db.add(cliente)
    db.commit()
    db.refresh(cliente)

    return RedirectResponse(url=f"/admin/clientes/{cliente.id}?message=Cliente cadastrado com sucesso.", status_code=303)


@admin_app.get("/clientes/{cliente_id}", response_class=HTMLResponse)
async def cliente_detail(request: Request, cliente_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    cliente = load_cliente(db, request, cliente_id)
    filtro_empresa_id = _query_empresa_id(request)
    if filtro_empresa_id and cliente.empresa_id != filtro_empresa_id:
        raise HTTPException(status_code=404, detail="cliente_nao_encontrado")

    empresa = load_empresa(db, request, cliente.empresa_id)
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


@admin_app.post("/clientes/{cliente_id}/email")
async def cliente_email_submit(request: Request, cliente_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    cliente = load_cliente(db, request, cliente_id)
    form = await request.form()
    cliente.email = parse_optional_str(form.get("email"))
    db.commit()

    return RedirectResponse(url=f"/admin/clientes/{cliente.id}?message=E-mail atualizado com sucesso.", status_code=303)


@admin_app.get("/solicitacoes-atendimento", response_class=HTMLResponse)
async def solicitacoes_atendimento_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    empresas = _empresas_visiveis(db, request)
    if not empresa_id and len(empresas) == 1:
        empresa_id = empresas[0].id

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
            .filter(SolicitacaoAtendimento.empresa_id == empresa_id)
            .filter(SolicitacaoAtendimento.status == STATUS_PENDENTE)
            .order_by(SolicitacaoAtendimento.criado_em.desc(), SolicitacaoAtendimento.id.desc())
        )
        registros = query.all()

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
            selected_empresa_id=empresa_id,
        ),
    )


@admin_app.post("/solicitacoes-atendimento/{solicitacao_id}/status")
async def solicitacao_atendimento_status_submit(request: Request, solicitacao_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    form = await request.form()
    status = (form.get("status") or "").strip()

    if status not in {STATUS_EM_ATENDIMENTO, STATUS_FINALIZADO}:
        raise HTTPException(status_code=400, detail="status_invalido")

    solicitacao = load_solicitacao(db, request, solicitacao_id)
    atualizar_status_solicitacao_atendimento(db, solicitacao, status)

    mensagem = "Solicitação atualizada com sucesso."
    return RedirectResponse(
        url=f"/admin/solicitacoes-atendimento?empresa_id={solicitacao.empresa_id}&message={mensagem}",
        status_code=303,
    )


@admin_app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    empresas, empresa = _base_empresa_contexto(db, request, empresa_id)
    frases = gerar_insights(db, empresa.id if empresa else empresa_id)

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
async def clientes_inativos_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    empresa_id = _query_empresa_id(request)
    try:
        dias = int(request.query_params.get("dias") or 90)
    except ValueError:
        dias = 90
    if dias not in DIAS_INATIVIDADE_PERMITIDOS:
        dias = 90

    empresas, empresa = _base_empresa_contexto(db, request, empresa_id)
    clientes = listar_clientes_inativos(db, empresa.id if empresa else empresa_id, dias)

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


@admin_app.get("/configuracoes", response_class=HTMLResponse)
async def configuracoes_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_superadmin)):
    config = obter_configuracao(db)

    return templates.TemplateResponse(
        request,
        "admin/configuracoes.html",
        page_context(
            request,
            title="Configurações",
            config=config,
            meta_token_configurado=bool(config.meta_token),
            ai_api_key_configurada=bool(config.ai_api_key),
            resend_api_key_configurada=bool(config.resend_api_key),
        ),
    )


@admin_app.post("/configuracoes")
async def configuracoes_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_superadmin)):
    form = await request.form()
    campos = {
        "meta_phone_number_id": (form.get("meta_phone_number_id") or "").strip(),
        "meta_business_id": parse_optional_str(form.get("meta_business_id")),
        "bot_activation_words_raw": (form.get("bot_activation_words_raw") or "oibot").strip(),
        "lembrete_antecedencia_horas": parse_optional_int(form.get("lembrete_antecedencia_horas")) or 24,
        "lembrete_intervalo_minutos": parse_optional_int(form.get("lembrete_intervalo_minutos")) or 15,
        "email_from_endereco": parse_optional_str(form.get("email_from_endereco")),
        "email_from_nome": parse_optional_str(form.get("email_from_nome")),
        "ai_enabled": parse_bool(form.get("ai_enabled")),
        "ai_provider": (form.get("ai_provider") or "openai").strip(),
        "ai_model": (form.get("ai_model") or "gpt-4o-mini").strip(),
        "ai_timeout_segundos": parse_optional_float(form.get("ai_timeout_segundos")) or 6.0,
        "ai_cache_ttl_segundos": parse_optional_int(form.get("ai_cache_ttl_segundos")) or 600,
    }

    meta_token_novo = (form.get("meta_token") or "").strip()
    if meta_token_novo:
        campos["meta_token"] = meta_token_novo

    ai_api_key_novo = (form.get("ai_api_key") or "").strip()
    if ai_api_key_novo:
        campos["ai_api_key"] = ai_api_key_novo

    resend_api_key_novo = (form.get("resend_api_key") or "").strip()
    if resend_api_key_novo:
        campos["resend_api_key"] = resend_api_key_novo

    atualizar_configuracao(db, **campos)

    return RedirectResponse(url="/admin/configuracoes?message=Configurações atualizadas com sucesso.", status_code=303)


@admin_app.get("/usuarios", response_class=HTMLResponse)
async def usuarios_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    empresas = _empresas_visiveis(db, request)
    if not empresa_id and len(empresas) == 1:
        empresa_id = empresas[0].id
    usuarios = listar_usuarios(db, empresa_id)

    return templates.TemplateResponse(
        request,
        "admin/usuarios_list.html",
        page_context(
            request,
            title="Usuários",
            empresas=empresas,
            usuarios=usuarios,
            selected_empresa_id=empresa_id,
        ),
    )


@admin_app.get("/usuarios/novo", response_class=HTMLResponse)
async def usuario_new_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresa_id = _query_empresa_id(request)
    empresas = _empresas_visiveis(db, request)

    return templates.TemplateResponse(
        request,
        "admin/usuario_form.html",
        page_context(
            request,
            title="Novo usuário",
            empresas=empresas,
            usuario=None,
            selected_empresa_id=empresa_id,
            action_url="/admin/usuarios/novo",
        ),
    )


@admin_app.post("/usuarios/novo")
async def usuario_new_submit(request: Request, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    form = await request.form()
    empresa_id = _empresa_id_do_formulario(request, form)
    load_empresa(db, request, empresa_id)
    nome = (form.get("nome") or "").strip()
    email = (form.get("email") or "").strip().lower()
    senha = form.get("senha") or ""
    papel = (form.get("papel") or PAPEL_OPERADOR).strip()

    erro = None
    if not nome:
        erro = "Informe o nome."
    elif not email:
        erro = "Informe o e-mail."
    elif len(senha) < 8:
        erro = "A senha deve ter pelo menos 8 caracteres."
    elif papel not in {PAPEL_ADMIN, PAPEL_OPERADOR}:
        erro = "Papel inválido."

    if not erro:
        try:
            criar_usuario(db, empresa_id=empresa_id, nome=nome, email=email, senha=senha, papel=papel)
        except IntegrityError:
            db.rollback()
            erro = "Já existe um usuário com esse e-mail."

    if erro:
        empresas = _empresas_visiveis(db, request)
        return templates.TemplateResponse(
            request,
            "admin/usuario_form.html",
            page_context(
                request,
                title="Novo usuário",
                empresas=empresas,
                usuario=SimpleNamespace(nome=nome, email=email, papel=papel, ativo=True),
                selected_empresa_id=empresa_id,
                action_url="/admin/usuarios/novo",
                error=erro,
            ),
            status_code=400,
        )

    return RedirectResponse(url=f"/admin/usuarios?empresa_id={empresa_id}&message=Usuário criado com sucesso.", status_code=303)


@admin_app.get("/usuarios/{usuario_id}/editar", response_class=HTMLResponse)
async def usuario_edit_page(request: Request, usuario_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    empresas = _empresas_visiveis(db, request)
    usuario = load_usuario(db, request, usuario_id)

    return templates.TemplateResponse(
        request,
        "admin/usuario_form.html",
        page_context(
            request,
            title="Editar usuário",
            empresas=empresas,
            usuario=usuario,
            selected_empresa_id=usuario.empresa_id,
            action_url=f"/admin/usuarios/{usuario_id}/editar",
        ),
    )


@admin_app.post("/usuarios/{usuario_id}/editar")
async def usuario_edit_submit(request: Request, usuario_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    form = await request.form()
    usuario = load_usuario(db, request, usuario_id)
    nome = (form.get("nome") or "").strip()
    papel = (form.get("papel") or PAPEL_OPERADOR).strip()
    ativo = parse_bool(form.get("ativo"))
    nova_senha = form.get("senha") or ""

    erro = None
    if not nome:
        erro = "Informe o nome."
    elif papel not in {PAPEL_ADMIN, PAPEL_OPERADOR}:
        erro = "Papel inválido."
    elif nova_senha and len(nova_senha) < 8:
        erro = "A senha deve ter pelo menos 8 caracteres."

    if erro:
        empresas = _empresas_visiveis(db, request)
        return templates.TemplateResponse(
            request,
            "admin/usuario_form.html",
            page_context(
                request,
                title="Editar usuário",
                empresas=empresas,
                usuario=usuario,
                selected_empresa_id=usuario.empresa_id,
                action_url=f"/admin/usuarios/{usuario_id}/editar",
                error=erro,
            ),
            status_code=400,
        )

    atualizar_usuario(db, usuario, nome=nome, papel=papel, ativo=ativo, nova_senha=nova_senha or None)

    return RedirectResponse(url=f"/admin/usuarios?empresa_id={usuario.empresa_id}&message=Usuário atualizado com sucesso.", status_code=303)


@admin_app.post("/usuarios/{usuario_id}/toggle")
async def usuario_toggle(request: Request, usuario_id: int, db: Session = Depends(get_db), _: None = Depends(require_papel_admin)):
    usuario = load_usuario(db, request, usuario_id)
    if request.session.get("usuario_id") == usuario.id:
        raise HTTPException(status_code=400, detail="nao_e_possivel_desativar_a_propria_conta")
    usuario.ativo = not usuario.ativo
    db.commit()
    empresa_id = usuario.empresa_id

    return RedirectResponse(url=f"/admin/usuarios?empresa_id={empresa_id}&message=Status do usuário atualizado.", status_code=303)
