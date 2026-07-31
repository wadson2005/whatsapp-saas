from config import settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = settings.database_url

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


try:
    from schema import ensure_schema

    ensure_schema()
except Exception:
    # The FastAPI startup path also runs the schema bootstrap. Keeping the import here
    # makes direct scripts more resilient without blocking the application boot.
    pass