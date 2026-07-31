from database import SessionLocal
from models import Empresa

db = SessionLocal()
empresa = db.query(Empresa).filter_by(slug="sorriso-feliz").first()
empresa.ativo = False
db.commit()
print(f"Bot da '{empresa.nome}' está PAUSADO.")
db.close()