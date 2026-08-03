import sys

from core.config import settings
from core.database import SessionLocal
from core.models import Empresa

db = SessionLocal()
try:
    empresa = db.query(Empresa).filter_by(slug=settings.seed_empresa_slug).first()
    if empresa is None:
        print(f"Nenhuma empresa encontrada com slug '{settings.seed_empresa_slug}'.")
        sys.exit(1)

    empresa.ativo = True
    db.commit()
    print(f"Bot da '{empresa.nome}' está ATIVO novamente.")
finally:
    db.close()
