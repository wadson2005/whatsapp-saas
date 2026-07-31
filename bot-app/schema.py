from sqlalchemy import text

from database import Base, engine
import models  # noqa: F401


def _add_column_if_missing(conn, table_name: str, column_ddl: str):
    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_ddl}"))


def ensure_schema():
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        _add_column_if_missing(conn, "empresas", "horario_abertura VARCHAR(5) DEFAULT '08:00'")
        _add_column_if_missing(conn, "empresas", "horario_fechamento VARCHAR(5) DEFAULT '18:00'")
        _add_column_if_missing(conn, "empresas", "intervalo_entre_atendimentos_minutos INTEGER DEFAULT 15")
        _add_column_if_missing(conn, "empresas", "dias_indisponiveis VARCHAR DEFAULT ''")
        _add_column_if_missing(conn, "empresas", "datas_indisponiveis VARCHAR DEFAULT ''")
        _add_column_if_missing(conn, "agendamentos", "fim_em TIMESTAMP")
        _add_column_if_missing(conn, "agendamentos", "duracao_minutos INTEGER DEFAULT 30")
        _add_column_if_missing(conn, "agendamentos", "cancelado_em TIMESTAMP")
        _add_column_if_missing(conn, "agendamentos", "motivo_cancelamento VARCHAR")