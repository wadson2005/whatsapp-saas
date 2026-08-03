def sql_bool(value: bool) -> str:
    """Render a boolean literal usable in raw DDL on both SQLite and PostgreSQL.

    SQLite accepts integers (0/1) as boolean defaults but PostgreSQL rejects
    them for BOOLEAN columns (type mismatch). TRUE/FALSE keywords work on
    both (SQLite >= 3.23), so all raw-SQL boolean defaults must go through
    this helper instead of hardcoding 1/0.
    """
    return "TRUE" if value else "FALSE"
