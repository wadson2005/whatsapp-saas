from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from .database import Base


class Empresa(Base):
    __tablename__ = "empresas"

    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    segmento = Column(String, nullable=False)  # clinica, barbearia, restaurante
    telefone_whatsapp = Column(String)
    email = Column(String)
    endereco = Column(String)
    descricao = Column(String)
    logo_url = Column(String)
    evolution_instance_name = Column(String)
    horario_abertura = Column(String, default="08:00")
    horario_fechamento = Column(String, default="18:00")
    horario_almoco_inicio = Column(String)
    horario_almoco_fim = Column(String)
    dias_funcionamento = Column(String, default="0,1,2,3,4,5")
    intervalo_entre_atendimentos_minutos = Column(Integer, default=15)
    dias_indisponiveis = Column(String, default="")
    datas_indisponiveis = Column(String, default="")
    atendimento_automatico_ativo = Column(Boolean, default=True)
    permitir_atendimento_humano = Column(Boolean, default=True)
    horario_resposta_inicio = Column(String, default="08:00")
    horario_resposta_fim = Column(String, default="18:00")
    mensagem_fora_horario = Column(String)
    tempo_max_conversa_minutos = Column(Integer, default=120)
    tempo_expiracao_contexto_minutos = Column(Integer, default=30)
    mensagem_boas_vindas = Column(String)
    mensagem_encerramento = Column(String)
    mensagem_atendimento_humano = Column(String)
    mensagem_sem_horarios = Column(String)
    mensagem_confirmacao = Column(String)
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    servicos = relationship("Servico", back_populates="empresa")
    agendamentos = relationship("Agendamento", back_populates="empresa")
    clientes_finais = relationship("ClienteFinal")
    solicitacoes_atendimento = relationship("SolicitacaoAtendimento", back_populates="empresa")


class Servico(Base):
    __tablename__ = "servicos"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    nome = Column(String, nullable=False)
    descricao = Column(String)
    duracao_minutos = Column(Integer, default=30)
    preco = Column(Float)
    ordem_exibicao = Column(Integer, default=0)
    ativo = Column(Boolean, default=True)
    excluido_em = Column(DateTime)

    empresa = relationship("Empresa", back_populates="servicos")


class ClienteFinal(Base):
    __tablename__ = "clientes_finais"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    telefone = Column(String, nullable=False)
    nome = Column(String)
    criado_em = Column(DateTime, default=datetime.utcnow)

    solicitacoes_atendimento = relationship("SolicitacaoAtendimento", back_populates="cliente")
    agendamentos = relationship("Agendamento", back_populates="cliente_final")


class UsuarioPainel(Base):
    __tablename__ = "usuarios_painel"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    nome = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    senha_hash = Column(String, nullable=False)
    papel = Column(String, nullable=False, default="operador")
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    empresa = relationship("Empresa")


class SolicitacaoAtendimento(Base):
    __tablename__ = "solicitacoes_atendimento"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes_finais.id"), nullable=True)
    telefone = Column(String, nullable=False)
    nome = Column(String)
    mensagem = Column(String, nullable=False)
    status = Column(String, default="pendente")
    criado_em = Column(DateTime, default=datetime.utcnow)

    empresa = relationship("Empresa", back_populates="solicitacoes_atendimento")
    cliente = relationship("ClienteFinal", back_populates="solicitacoes_atendimento")


class Agendamento(Base):
    __tablename__ = "agendamentos"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    cliente_final_id = Column(Integer, ForeignKey("clientes_finais.id"), nullable=False)
    servico_id = Column(Integer, ForeignKey("servicos.id"), nullable=False)
    data_hora = Column(DateTime, nullable=False)
    fim_em = Column(DateTime)
    duracao_minutos = Column(Integer, default=30)
    status = Column(String, default="agendado")  # agendado, confirmado, concluido, cancelado
    cancelado_em = Column(DateTime)
    motivo_cancelamento = Column(String)
    lembrete_enviado_em = Column(DateTime)

    empresa = relationship("Empresa", back_populates="agendamentos")
    servico = relationship("Servico")
    cliente_final = relationship("ClienteFinal")


class EmpresaConhecimento(Base):
    __tablename__ = "empresa_conhecimento"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    categoria = Column(String)
    pergunta = Column(String, nullable=False)
    resposta = Column(String, nullable=False)
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    excluido_em = Column(DateTime)

    empresa = relationship("Empresa")


class ConversaIniciada(Base):
    __tablename__ = "conversas_iniciadas"

    id = Column(Integer, primary_key=True)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), nullable=False)
    telefone = Column(String, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)

    empresa = relationship("Empresa")


class ConfiguracaoSistema(Base):
    """Linha única (id=1) com as configurações operacionais editáveis pelo painel.

    Os valores do .env (config.py) só servem de valor inicial, copiado pra cá na
    primeira leitura — depois disso, esta tabela é a fonte viva.
    """

    __tablename__ = "configuracao_sistema"

    id = Column(Integer, primary_key=True)

    meta_token = Column(String)
    meta_phone_number_id = Column(String)
    meta_business_id = Column(String)

    bot_activation_words_raw = Column(String, default="oibot")

    meta_template_lembrete_nome = Column(String, default="lembrete_agendamento")
    meta_template_lembrete_idioma = Column(String, default="pt_BR")
    lembrete_antecedencia_horas = Column(Integer, default=24)
    lembrete_intervalo_minutos = Column(Integer, default=15)

    ai_enabled = Column(Boolean, default=False)
    ai_provider = Column(String, default="openai")
    ai_api_key = Column(String)
    ai_model = Column(String, default="gpt-4o-mini")
    ai_timeout_segundos = Column(Float, default=6.0)
    ai_cache_ttl_segundos = Column(Integer, default=600)

    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)