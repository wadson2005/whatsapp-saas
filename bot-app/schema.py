from sqlalchemy import inspect, text

import models  # noqa: F401
from database import Base, engine
from db_compat import sql_bool


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
        _add_column_if_missing(conn, "agendamentos", "lembrete_enviado_em TIMESTAMP")