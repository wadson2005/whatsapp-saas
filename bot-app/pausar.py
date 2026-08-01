from config import settings
from database import SessionLocal
from models import Empresa

db = SessionLocal()
empresa = db.query(Empresa).filter_by(slug=settings.seed_empresa_slug).first()
empresa.ativo = False
db.commit()
print(f"Bot da '{empresa.nome}' está PAUSADO.")
db.close()