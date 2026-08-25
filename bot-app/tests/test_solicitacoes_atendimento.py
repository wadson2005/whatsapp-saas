import importlib
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from conftest import WEBHOOK_SECRET, FakeRedis, preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    conversa = importlib.import_module("conversa")
    models = importlib.import_module("core.models")
    atendimento_humano = importlib.import_module("services.atendimento_humano")
    main.ensure_schema()
    return main, conversa, models, atendimento_humano


def _payload_texto(instance: str, numero: str, texto: str) -> dict:
    return {
        "instance": instance,
        "data": {
            "key": {"fromMe": False, "remoteJid": numero},
            "message": {"conversation": texto},
        },
    }


def _seed_empresa(main, models, slug: str, nome: str, telefone: str, instancia: str):
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome=nome,
            slug=slug,
            segmento="clinica",
            telefone_whatsapp=telefone,
            evolution_instance_name=instancia,
            horario_abertura="08:00",
            horario_fechamento="18:00",
            intervalo_entre_atendimentos_minutos=15,
            ativo=True,
        )
        db.add(empresa)
        db.commit()
        db.refresh(empresa)
        return empresa
    finally:
        db.close()


def _seed_solicitacao(main, models, empresa, telefone: str, nome: str, mensagem: str, criado_em: datetime):
    db = main.SessionLocal()
    try:
        cliente = models.ClienteFinal(empresa_id=empresa.id, telefone=telefone, nome=nome)
        db.add(cliente)
        db.flush()
        solicitacao = models.SolicitacaoAtendimento(
            empresa_id=empresa.id,
            cliente_id=cliente.id,
            telefone=telefone,
            nome=nome,
            mensagem=mensagem,
            status="pendente",
            criado_em=criado_em,
        )
        db.add(solicitacao)
        db.commit()
        db.refresh(solicitacao)
        return solicitacao
    finally:
        db.close()


def _login_admin(client: TestClient):
    resposta = client.post(
        "/admin/login",
        data={"username": "admin", "password": "senha-super-segura-123"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303


def test_solicitacao_humana_eh_criada_e_confirmada_no_whatsapp(monkeypatch, tmp_path):
    main, conversa, models, _ = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    empresa = _seed_empresa(main, models, "clinica-sorriso-feliz", "Clínica Sorriso Feliz", "5586999999999", "clinica-sorriso-feliz")

    with TestClient(main.app, base_url="https://testserver") as client:
        response = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999999", "quero falar com atendente"),
        )

    assert response.status_code == 200
    assert conversa.enviar_botoes.await_count == 1
    assert "solicitação" in conversa.enviar_botoes.await_args.kwargs["texto"].lower()

    db = main.SessionLocal()
    try:
        solicitacoes = db.query(models.SolicitacaoAtendimento).filter_by(empresa_id=empresa.id).all()
    finally:
        db.close()

    assert len(solicitacoes) == 1
    assert solicitacoes[0].status == "pendente"
    assert solicitacoes[0].telefone == "5586999999999"
    assert "atendente" in solicitacoes[0].mensagem.lower()


def test_solicitacao_humana_nao_duplica_pendente(monkeypatch, tmp_path):
    main, conversa, models, _ = carregar_app(monkeypatch, tmp_path)
    conversa.redis_cliente = FakeRedis()
    conversa.enviar_botoes = AsyncMock()
    conversa.enviar_lista = AsyncMock()

    _seed_empresa(main, models, "clinica-sorriso-feliz", "Clínica Sorriso Feliz", "5586999999998", "clinica-sorriso-feliz")

    with TestClient(main.app, base_url="https://testserver") as client:
        primeira = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999998", "falar com humano"),
        )
        segunda = client.post(
            f"/webhook?token={WEBHOOK_SECRET}",
            json=_payload_texto("clinica-sorriso-feliz", "5586999999998", "falar com humano"),
        )

    assert primeira.status_code == 200
    assert segunda.status_code == 200
    assert "já está registrada" in conversa.enviar_botoes.await_args.kwargs["texto"].lower()

    db = main.SessionLocal()
    try:
        total = db.query(models.SolicitacaoAtendimento).count()
    finally:
        db.close()

    assert total == 1


def test_admin_altera_status_e_isola_por_empresa(monkeypatch, tmp_path):
    main, _, models, _ = carregar_app(monkeypatch, tmp_path)

    empresa_a = _seed_empresa(main, models, "clinica-a", "Clínica A", "5511999999991", "instancia-a")
    empresa_b = _seed_empresa(main, models, "clinica-b", "Clínica B", "5511999999992", "instancia-b")

    _seed_solicitacao(main, models, empresa_a, "5511999999991", "Ana", "Mensagem antiga", datetime(2026, 7, 31, 10, 0))
    _seed_solicitacao(main, models, empresa_a, "5511999999993", "Bruno", "Mensagem recente", datetime(2026, 7, 31, 11, 0))
    _seed_solicitacao(main, models, empresa_b, "5511999999992", "Carla", "Outra empresa", datetime(2026, 7, 31, 12, 0))

    with TestClient(main.app, base_url="https://testserver") as client:
        _login_admin(client)

        resposta_lista = client.get(f"/admin/solicitacoes-atendimento?empresa_id={empresa_a.id}")
        assert resposta_lista.status_code == 200
        assert "Mensagem recente" in resposta_lista.text
        assert "Mensagem antiga" in resposta_lista.text
        assert "Outra empresa" not in resposta_lista.text
        assert resposta_lista.text.index("Mensagem recente") < resposta_lista.text.index("Mensagem antiga")

        db = main.SessionLocal()
        try:
            solicitacao = (
                db.query(models.SolicitacaoAtendimento)
                .filter_by(empresa_id=empresa_a.id)
                .order_by(models.SolicitacaoAtendimento.criado_em.desc())
                .first()
            )
            solicitacao_id = solicitacao.id
        finally:
            db.close()

        resposta_status = client.post(
            f"/admin/solicitacoes-atendimento/{solicitacao_id}/status?empresa_id={empresa_a.id}",
            data={"status": "em_atendimento"},
            follow_redirects=False,
        )
        assert resposta_status.status_code == 303

        db = main.SessionLocal()
        try:
            atualizada = db.query(models.SolicitacaoAtendimento).filter_by(id=solicitacao_id).first()
        finally:
            db.close()

        assert atualizada.status == "em_atendimento"

        resposta_finalizar = client.post(
            f"/admin/solicitacoes-atendimento/{solicitacao_id}/status?empresa_id={empresa_a.id}",
            data={"status": "finalizado"},
            follow_redirects=False,
        )
        assert resposta_finalizar.status_code == 303

        db = main.SessionLocal()
        try:
            finalizada = db.query(models.SolicitacaoAtendimento).filter_by(id=solicitacao_id).first()
        finally:
            db.close()

        assert finalizada.status == "finalizado"
