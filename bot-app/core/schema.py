import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from . import models  # noqa: F401
from .database import Base, engine
from .db_compat import sql_bool

logger = logging.getLogger(__name__)


def _add_column_if_missing(conn, table_name: str, column_ddl: str):
    column_name = column_ddl.split()[0]
    existing_columns = {column["name"] for column in inspect(conn).get_columns(table_name)}
    if column_name not in existing_columns:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_ddl}"))


def ensure_schema():
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        _add_column_if_missing(conn, "empresas", "email VARCHAR")
        _add_column_if_missing(conn, "empresas", "endereco VARCHAR")
        _add_column_if_missing(conn, "empresas", "descricao VARCHAR")
        _add_column_if_missing(conn, "empresas", "logo_url VARCHAR")
        _add_column_if_missing(conn, "empresas", "horario_abertura VARCHAR(5) DEFAULT '08:00'")
        _add_column_if_missing(conn, "empresas", "horario_fechamento VARCHAR(5) DEFAULT '18:00'")
        _add_column_if_missing(conn, "empresas", "horario_almoco_inicio VARCHAR(5)")
        _add_column_if_missing(conn, "empresas", "horario_almoco_fim VARCHAR(5)")
        _add_column_if_missing(conn, "empresas", "dias_funcionamento VARCHAR DEFAULT '0,1,2,3,4,5'")
        _add_column_if_missing(conn, "empresas", "intervalo_entre_atendimentos_minutos INTEGER DEFAULT 15")
        _add_column_if_missing(conn, "empresas", "dias_indisponiveis VARCHAR DEFAULT ''")
        _add_column_if_missing(conn, "empresas", "datas_indisponiveis VARCHAR DEFAULT ''")
        _add_column_if_missing(conn, "empresas", "palavra_ativacao VARCHAR DEFAULT 'oibot'")
        _add_column_if_missing(
            conn, "empresas", f"atendimento_automatico_ativo BOOLEAN DEFAULT {sql_bool(True)}"
        )
        _add_column_if_missing(
            conn, "empresas", f"permitir_atendimento_humano BOOLEAN DEFAULT {sql_bool(True)}"
        )
        _add_column_if_missing(conn, "empresas", "horario_resposta_inicio VARCHAR(5) DEFAULT '08:00'")
        _add_column_if_missing(conn, "empresas", "horario_resposta_fim VARCHAR(5) DEFAULT '18:00'")
        _add_column_if_missing(conn, "empresas", "mensagem_fora_horario VARCHAR")
        _add_column_if_missing(conn, "empresas", "tempo_max_conversa_minutos INTEGER DEFAULT 120")
        _add_column_if_missing(conn, "empresas", "tempo_expiracao_contexto_minutos INTEGER DEFAULT 30")
        _add_column_if_missing(conn, "empresas", "mensagem_boas_vindas VARCHAR")
        _add_column_if_missing(conn, "empresas", "mensagem_encerramento VARCHAR")
        _add_column_if_missing(conn, "empresas", "mensagem_atendimento_humano VARCHAR")
        _add_column_if_missing(conn, "empresas", "mensagem_sem_horarios VARCHAR")
        _add_column_if_missing(conn, "empresas", "mensagem_confirmacao VARCHAR")
        _add_column_if_missing(conn, "servicos", "descricao VARCHAR")
        _add_column_if_missing(conn, "servicos", "ordem_exibicao INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "servicos", "excluido_em TIMESTAMP")
        _add_column_if_missing(conn, "agendamentos", "fim_em TIMESTAMP")
        _add_column_if_missing(conn, "agendamentos", "duracao_minutos INTEGER DEFAULT 30")
        _add_column_if_missing(conn, "agendamentos", "cancelado_em TIMESTAMP")
        _add_column_if_missing(conn, "agendamentos", "motivo_cancelamento VARCHAR")
        _add_column_if_missing(conn, "agendamentos", "lembrete_email_enviado_em TIMESTAMP")
        _add_column_if_missing(conn, "usuarios_painel", "reset_token_hash VARCHAR")
        _add_column_if_missing(conn, "usuarios_painel", "reset_token_expira_em TIMESTAMP")
        _add_column_if_missing(conn, "empresas", "ativado_em TIMESTAMP")
        _add_column_if_missing(
            conn, "empresas", f"lembrete_canal_email BOOLEAN DEFAULT {sql_bool(False)}"
        )
        _add_column_if_missing(conn, "clientes_finais", "email VARCHAR")
        _add_column_if_missing(conn, "configuracao_sistema", "resend_api_key VARCHAR")
        _add_column_if_missing(conn, "configuracao_sistema", "email_from_endereco VARCHAR")
        _add_column_if_missing(conn, "configuracao_sistema", "email_from_nome VARCHAR")
        _add_column_if_missing(conn, "empresas", "estado_conexao_whatsapp VARCHAR")
        _add_column_if_missing(conn, "empresas", "estado_conexao_atualizado_em TIMESTAMP")
        _backfill_ativado_em(conn)
        _permitir_usuario_sem_empresa(conn)
        _adicionar_unique_telefone_whatsapp(conn)


def _backfill_ativado_em(conn):
    """Preenche `ativado_em` pra empresas que já estavam ativas antes desse campo existir.

    Sem isso, uma empresa que já estava no ar (ex.: em produção antes desse
    deploy) ficaria com `ativado_em=NULL` — e a primeira vez que fosse pausada
    pareceria "nunca configurada" em vez de "pausada". Idempotente: só afeta
    linhas com `ativado_em` ainda nulo, então rodar de novo não faz nada.
    """
    conn.execute(
        text("UPDATE empresas SET ativado_em = criado_em WHERE ativo = :ativo AND ativado_em IS NULL"),
        {"ativo": True},
    )


def _permitir_usuario_sem_empresa(conn):
    """Remove o NOT NULL de usuarios_painel.empresa_id em bancos já existentes.

    Necessário para o fluxo de conta separada de empresa (usuário se cadastra
    antes de ter uma empresa). SQLite não suporta ALTER COLUMN — mas bancos
    SQLite (sempre recriados do zero nos testes) já nascem com a coluna
    nullable a partir do modelo atual, então essa migration só faz sentido
    (e só é aplicada) em PostgreSQL.
    """
    if conn.dialect.name != "postgresql":
        return
    conn.execute(text("ALTER TABLE usuarios_painel ALTER COLUMN empresa_id DROP NOT NULL"))


def _adicionar_unique_telefone_whatsapp(conn):
    """Impede duas empresas com o mesmo `telefone_whatsapp` — proteção real contra
    duas empresas conectando o mesmo número (a validação da rota é a primeira
    linha de defesa, isso aqui é a garantia contra corrida entre requisições
    simultâneas).

    SQLite (testes) já nasce com a constraint via `unique=True` no modelo. Em
    Postgres precisa de ALTER TABLE — idempotente (checa se já existe antes) e
    nunca derruba o boot: se já houver telefones duplicados em produção (dado
    anterior a essa migration), a constraint falha e o erro fica só registrado
    no log, sem impedir o resto da aplicação de subir. Nesse caso a duplicidade
    precisa ser resolvida manualmente antes da constraint conseguir ser criada.
    """
    if conn.dialect.name != "postgresql":
        return
    existe = conn.execute(
        text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'uq_empresas_telefone_whatsapp' AND conrelid = 'empresas'::regclass"
        )
    ).first()
    if existe:
        logger.debug("Constraint uq_empresas_telefone_whatsapp já existe em empresas — nada a fazer.")
        return
    try:
        with conn.begin_nested():  # savepoint: uma falha aqui não derruba as outras migrations desta transação
            conn.execute(
                text("ALTER TABLE empresas ADD CONSTRAINT uq_empresas_telefone_whatsapp UNIQUE (telefone_whatsapp)")
            )
    except DBAPIError:
        logger.error(
            "Não foi possível criar a constraint UNIQUE em empresas.telefone_whatsapp — "
            "provavelmente já existem números duplicados no banco. A validação da rota "
            "(admin.py) continua ativa sozinha até isso ser resolvido manualmente e o "
            "processo ser reiniciado."
        )
    else:
        logger.info("Constraint uq_empresas_telefone_whatsapp criada com sucesso em empresas.telefone_whatsapp.")