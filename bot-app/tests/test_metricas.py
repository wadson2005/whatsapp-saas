import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_ENV = {
    "REDIS_URL": "redis://localhost:6379/1",
    "EVOLUTION_API_KEY": "x",
    "META_TOKEN": "x",
    "META_PHONE_NUMBER_ID": "x",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "senha-super-segura-123",
    "SESSION_SECRET_KEY": "0123456789abcdef0123456789abcdef",
}

MODULOS = [
    "main", "admin", "config", "database", "models", "schema", "conversa",
    "redis_client", "agenda", "meta_client", "atendimento_humano", "lembretes",
    "ai", "ai.provider", "ai.service", "ai.prompts", "ai.models", "ai.cache",
    "texto_utils", "conhecimento", "metricas", "configuracoes",
]


def carregar_app(monkeypatch, tmp_path: Path):
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    database_path = tmp_path / "bot-app.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    for chave, valor in BOOTSTRAP_ENV.items():
        monkeypatch.setenv(chave, valor)

    for modulo in MODULOS:
        sys.modules.pop(modulo, None)

    main = importlib.import_module("main")
    models = importlib.import_module("models")
    metricas = importlib.import_module("metricas")
    main.ensure_schema()
    return main, models, metricas


def _seed_empresa(main, models, slug: str, nome: str):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome=nome,
            slug=slug,
            segmento="clinica",
            telefone_whatsapp="5511999999990",
            evolution_instance_name=slug,
            horario_abertura="08:00",
            horario_fechamento="18:00",
            intervalo_entre_atendimentos_minutos=15,
            ativo=True,
        )
        db.add(empresa)
        db.commit()
        db.refresh(empresa)
        return empresa
    finally:
        db.close()


def _seed_servico(main, models, empresa, nome: str, duracao: int = 30):
    db = main.SessionLocal()
    try:
        servico = models.Servico(empresa_id=empresa.id, nome=nome, duracao_minutos=duracao, ativo=True)
        db.add(servico)
        db.commit()
        db.refresh(servico)
        return servico
    finally:
        db.close()


def _seed_cliente(main, models, empresa, telefone: str, nome: str, criado_em: datetime | None = None):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome, criado_em=criado_em or datetime.utcnow())
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
        return cliente
    finally:
        db.close()


def _seed_agendamento(main, models, empresa, cliente, servico, data_hora, status="agendado", cancelado_em=None):
    db = main.SessionLocal()
    try:
        agendamento = models.Agendamento(
            empresa_id=empresa.id,
            cliente_final_id=cliente.id,
            servico_id=servico.id,
            data_hora=data_hora,
            duracao_minutos=servico.duracao_minutos,
            status=status,
            cancelado_em=cancelado_em,
        )
        db.add(agendamento)
        db.commit()
        db.refresh(agendamento)
        return agendamento
    finally:
        db.close()


def _seed_conversa(main, models, empresa, telefone: str, criado_em: datetime | None = None):
    db = main.SessionLocal()
    try:
        conversa = models.ConversaIniciada(empresa_id=empresa.id, telefone=telefone, criado_em=criado_em or datetime.utcnow())
        db.add(conversa)
        db.commit()
    finally:
        db.close()


def test_calcular_metricas_conta_conversas_agendamentos_cancelamentos_solicitacoes(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    servico = _seed_servico(main, models, empresa, "Corte")
    agora = datetime.utcnow()

    cliente1 = _seed_cliente(main, models, empresa, "5511900000001", "Ana")
    cliente2 = _seed_cliente(main, models, empresa, "5511900000002", "Bruno")

    _seed_conversa(main, models, empresa, "5511900000001", agora - timedelta(days=1))
    _seed_conversa(main, models, empresa, "5511900000002", agora - timedelta(days=2))
    _seed_conversa(main, models, empresa, "5511900000003", agora - timedelta(days=40))  # fora do período

    _seed_agendamento(main, models, empresa, cliente1, servico, agora - timedelta(days=1), status="agendado")
    _seed_agendamento(
        main, models, empresa, cliente2, servico, agora - timedelta(days=2),
        status="cancelado", cancelado_em=agora - timedelta(hours=5),
    )

    db = main.SessionLocal()
    try:
        solicitacao = models.SolicitacaoAtendimento(
            empresa_id=empresa.id, telefone="5511900000001", mensagem="Preciso de ajuda", status="pendente",
            criado_em=agora - timedelta(hours=1),
        )
        db.add(solicitacao)
        db.commit()
    finally:
        db.close()

    db = main.SessionLocal()
    try:
        resultado = metricas.calcular_metricas(db, empresa.id, agora - timedelta(days=30), agora)
    finally:
        db.close()

    assert resultado.conversas_iniciadas == 2
    assert resultado.agendamentos_realizados == 1
    assert resultado.cancelamentos == 1
    assert resultado.solicitacoes_atendimento == 1
    assert resultado.taxa_conversao == 0.5


def test_taxa_conversao_zero_sem_conversas(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")

    db = main.SessionLocal()
    try:
        resultado = metricas.calcular_metricas(db, empresa.id, datetime.utcnow() - timedelta(days=30), datetime.utcnow())
    finally:
        db.close()

    assert resultado.taxa_conversao == 0.0


def test_servico_horario_e_dia_mais_solicitados(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    corte = _seed_servico(main, models, empresa, "Corte")
    barba = _seed_servico(main, models, empresa, "Barba")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana")

    # segunda-feira 10h, duas vezes; terça-feira 15h, uma vez
    segunda_10h = datetime(2026, 8, 10, 10, 0)  # segunda-feira
    segunda_10h_2 = datetime(2026, 8, 17, 10, 0)  # também segunda
    terca_15h = datetime(2026, 8, 11, 15, 0)

    _seed_agendamento(main, models, empresa, cliente, corte, segunda_10h)
    _seed_agendamento(main, models, empresa, cliente, corte, segunda_10h_2)
    _seed_agendamento(main, models, empresa, cliente, barba, terca_15h)

    db = main.SessionLocal()
    try:
        resultado = metricas.calcular_metricas(db, empresa.id, datetime(2026, 8, 1), datetime(2026, 8, 31))
    finally:
        db.close()

    assert resultado.servico_mais_solicitado == "Corte"
    assert resultado.servico_mais_solicitado_percentual > 50
    assert resultado.horario_mais_solicitado == "10h"
    assert resultado.dia_semana_mais_movimentado == "Segunda-feira"


def test_clientes_novos_e_recorrentes(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    servico = _seed_servico(main, models, empresa, "Corte")
    agora = datetime.utcnow()

    cliente_novo = _seed_cliente(main, models, empresa, "5511900000001", "Ana", criado_em=agora - timedelta(days=2))
    cliente_antigo = _seed_cliente(main, models, empresa, "5511900000002", "Bruno", criado_em=agora - timedelta(days=200))

    _seed_agendamento(main, models, empresa, cliente_novo, servico, agora - timedelta(days=1))
    _seed_agendamento(main, models, empresa, cliente_antigo, servico, agora - timedelta(days=100))
    _seed_agendamento(main, models, empresa, cliente_antigo, servico, agora - timedelta(days=1))

    db = main.SessionLocal()
    try:
        resultado = metricas.calcular_metricas(db, empresa.id, agora - timedelta(days=30), agora)
    finally:
        db.close()

    assert resultado.clientes_novos == 1
    assert resultado.clientes_recorrentes == 1


def test_gerar_insights_com_dados_suficientes(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")
    servico = _seed_servico(main, models, empresa, "Limpeza")
    cliente = _seed_cliente(main, models, empresa, "5511900000001", "Ana")
    agora = datetime.utcnow()

    _seed_agendamento(main, models, empresa, cliente, servico, agora - timedelta(days=1))

    db = main.SessionLocal()
    try:
        frases = metricas.gerar_insights(db, empresa.id)
    finally:
        db.close()

    assert any("Limpeza" in frase for frase in frases)


def test_gerar_insights_vazio_quando_sem_dados(monkeypatch, tmp_path):
    main, models, metricas = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa(main, models, "clinica-a", "Clínica A")

    db = main.SessionLocal()
    try:
        frases = metricas.gerar_insights(db, empresa.id)
    finally:
        db.close()

    assert frases == []
