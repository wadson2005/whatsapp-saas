from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from database import Base


class Empresa(Base):
    __tablename__ = "empresas"

    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    segmento = Column(String, nullable=False)  # clinica, barbearia, restaurante
    telefone_whatsapp = Column(String)
    evolution_instance_name = Column(String)
    horario_abertura = Column(String, default="08:00")
    horario_fechamento = Column(String, default="18:00")
    intervalo_entre_atendimentos_minutos = Column(Integer, default=15)
    dias_indisponiveis = Column(String, default="")
    datas_indisponiveis = Column(String, default="")
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    servicos = relationship("Servico", back_populates="empresa")
    agendamentos = relationship("Agendamento", back_populates="empresa")


class Servico(Base):
    __tablename__ = "servicos"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    nome = Column(String, nullable=False)
    duracao_minutos = Column(Integer, default=30)
    preco = Column(Float)
    ativo = Column(Boolean, default=True)

    empresa = relationship("Empresa", back_populates="servicos")


class ClienteFinal(Base):
    __tablename__ = "clientes_finais"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    telefone = Column(String, nullable=False)
    nome = Column(String)
    criado_em = Column(DateTime, default=datetime.utcnow)


class Agendamento(Base):
    __tablename__ = "agendamentos"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    cliente_final_id = Column(Integer, ForeignKey("clientes_finais.id"), nullable=False)
    servico_id = Column(Integer, ForeignKey("servicos.id"), nullable=False)
    data_hora = Column(DateTime, nullable=False)
    fim_em = Column(DateTime)
    duracao_minutos = Column(Integer, default=30)
    status = Column(String, default="pendente")  # pendente, confirmado, cancelado, realizado
    cancelado_em = Column(DateTime)
    motivo_cancelamento = Column(String)

    empresa = relationship("Empresa", back_populates="agendamentos")
    servico = relationship("Servico")
    cliente_final = relationship("ClienteFinal")