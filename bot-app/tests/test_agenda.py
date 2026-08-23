import importlib
from datetime import datetime, timedelta
from pathlib import Path

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("core.models")
    agenda = importlib.import_module("services.agenda")
    main.ensure_schema()
    return main, models, agenda


def _seed_empresa_e_servico(main, models):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome="Clínica Sorriso Feliz",
            slug="clinica-sorriso-feliz",
            segmento="clinica",
            telefone_whatsapp="5586999999990",
            evolution_instance_name="clinica-sorriso-feliz",
            horario_abertura="08:00",
            horario_fechamento="18:00",
            intervalo_entre_atendimentos_minutos=15,
            ativo=True,
        )
        db.add(empresa)
        db.flush()

        servico = models.Servico(empresa_id=empresa.id, nome="Consulta", duracao_minutos=30, ativo=True)
        db.add(servico)
        db.commit()
        db.refresh(empresa)
        db.refresh(servico)
        return empresa, servico
    finally:
        db.close()


def _proximo_horario_util(agenda) -> datetime:
    amanha = (agenda._agora() + timedelta(days=1)).date()
    return datetime(amanha.year, amanha.month, amanha.day, 10, 0)


def test_agendar_servico_cria_agendamento_em_horario_livre(monkeypatch, tmp_path):
    main, models, agenda = carregar_app(monkeypatch, tmp_path)
    empresa, servico = _seed_empresa_e_servico(main, models)
    inicio = _proximo_horario_util(agenda)

    db = main.SessionLocal()
    try:
        agendamento, validacao = agenda.agendar_servico(db, empresa, servico, "5586988887777", inicio)
    finally:
        db.close()

    assert validacao.ok
    assert agendamento is not None
    assert agendamento.data_hora == inicio


def test_agendar_servico_rejeita_conflito_criado_entre_validacao_e_trava(monkeypatch, tmp_path):
    """Regressão: duas mensagens 'simultâneas' pedindo o mesmo horário não podem
    gerar dois agendamentos. Simula a corrida inserindo um agendamento
    concorrente logo depois da primeira validação (antes da trava/segunda
    validação), como se outra transação tivesse comitado nesse intervalo."""
    main, models, agenda = carregar_app(monkeypatch, tmp_path)
    empresa, servico = _seed_empresa_e_servico(main, models)
    inicio = _proximo_horario_util(agenda)

    original_validar = agenda.validar_agendamento
    chamadas = {"n": 0}

    db = main.SessionLocal()
    try:
        cliente_concorrente = models.ClienteFinal(empresa_id=empresa.id, telefone="5586977776666")
        db.add(cliente_concorrente)
        db.flush()

        def validar_com_corrida(db_, empresa_, servico_, inicio_em, ignorar_agendamento_id=None):
            chamadas["n"] += 1
            resultado = original_validar(db_, empresa_, servico_, inicio_em, ignorar_agendamento_id=ignorar_agendamento_id)
            if chamadas["n"] == 1:
                concorrente = models.Agendamento(
                    empresa_id=empresa.id,
                    cliente_final_id=cliente_concorrente.id,
                    servico_id=servico.id,
                    data_hora=inicio,
                    fim_em=inicio + timedelta(minutes=30),
                    duracao_minutos=30,
                    status="agendado",
                )
                db_.add(concorrente)
                db_.commit()
            return resultado

        monkeypatch.setattr(agenda, "validar_agendamento", validar_com_corrida)

        agendamento, validacao = agenda.agendar_servico(db, empresa, servico, "5586988887777", inicio)
    finally:
        db.close()

    assert chamadas["n"] == 2
    assert agendamento is None
    assert not validacao.ok

    db = main.SessionLocal()
    try:
        assert db.query(models.Agendamento).count() == 1
    finally:
        db.close()
