from database import SessionLocal
from models import Empresa

db = SessionLocal()
empresa = db.query(Empresa).filter_by(slug="sorriso-feliz").first()
empresa.ativo = True
db.commit()
print(f"Bot da '{empresa.nome}' está ATIVO novamente.")
db.close()