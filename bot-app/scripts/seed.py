from config import settings
from database import SessionLocal
from models import Empresa, Servico

db = SessionLocal()

empresa_existente = db.query(Empresa).filter_by(slug=settings.seed_empresa_slug).first()

if empresa_existente:
    print("Empresa de teste já existe, nada a fazer.")
else:
    empresa = Empresa(
        nome=settings.seed_empresa_nome,
        slug=settings.seed_empresa_slug,
        segmento=settings.seed_empresa_segmento,
        telefone_whatsapp=settings.seed_empresa_telefone_whatsapp,
        evolution_instance_name=settings.seed_empresa_evolution_instance_name,
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