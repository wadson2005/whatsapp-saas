from database import SessionLocal
from models import Empresa, Servico

db = SessionLocal()

empresa_existente = db.query(Empresa).filter_by(slug="sorriso-feliz").first()

if empresa_existente:
    print("Empresa de teste já existe, nada a fazer.")
else:
    empresa = Empresa(
        nome="Clínica Sorriso Feliz",
        slug="sorriso-feliz",
        segmento="clinica",
        telefone_whatsapp="5586999999999",
        evolution_instance_name="teste-aprendizado",
    )
    db.add(empresa)
    db.commit()
    db.refresh(empresa)

    servicos = [
        Servico(empresa_id=empresa.id, nome="Consulta de avaliação", duracao_minutos=30, preco=80.0),
        Servico(empresa_id=empresa.id, nome="Limpeza dental", duracao_minutos=45, preco=150.0),
        Servico(empresa_id=empresa.id, nome="Clareamento", duracao_minutos=60, preco=350.0),
    ]
    db.add_all(servicos)
    db.commit()

    print(f"Empresa '{empresa.nome}' criada com sucesso, ID={empresa.id}")
    print(f"{len(servicos)} serviços cadastrados.")

db.close()