import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import WEBHOOK_SECRET, preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    main.ensure_schema()
    return main


def _payload():
    return {
        "instance": "empresa-inexistente",
        "data": {
            "key": {"fromMe": False, "remoteJid": "5586999999999"},
            "message": {"conversation": "oi"},
        },
    }


def test_webhook_sem_token_e_rejeitado(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/webhook", json=_payload())

    assert resposta.status_code == 401


def test_webhook_com_token_invalido_e_rejeitado(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post("/webhook?token=token-forjado", json=_payload())

    assert resposta.status_code == 401


def test_webhook_com_token_correto_e_processado(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(f"/webhook?token={WEBHOOK_SECRET}", json=_payload())

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "empresa_nao_encontrada"}


# --- extrair_conteudo: formato real da Evolution API (Baileys), confirmado no ------
# --- código-fonte da instância em produção (main.mjs.map da v2.3.6) ---------------


def test_extrair_conteudo_texto_simples(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    texto, id_interacao = main.extrair_conteudo({"message": {"conversation": "oibot"}})

    assert texto == "oibot"
    assert id_interacao is None


def test_extrair_conteudo_clique_em_botao_formato_evolution(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    payload = {
        "message": {
            "buttonsResponseMessage": {
                "selectedButtonId": "menu",
                "selectedDisplayText": "Menu",
            }
        }
    }
    texto, id_interacao = main.extrair_conteudo(payload)

    assert texto == "Menu"
    assert id_interacao == "menu"


def test_extrair_conteudo_clique_em_lista_formato_evolution(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    payload = {
        "message": {
            "listResponseMessage": {
                "title": "Corte de cabelo",
                "singleSelectReply": {"selectedRowId": "servico:4"},
            }
        }
    }
    texto, id_interacao = main.extrair_conteudo(payload)

    assert texto == "Corte de cabelo"
    assert id_interacao == "servico:4"


def test_extrair_conteudo_clique_em_lista_sem_title_usa_o_id(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    payload = {"message": {"listResponseMessage": {"singleSelectReply": {"selectedRowId": "servico:4"}}}}
    texto, id_interacao = main.extrair_conteudo(payload)

    assert texto == "servico:4"
    assert id_interacao == "servico:4"


def test_extrair_conteudo_formato_meta_legado_ainda_reconhecido(monkeypatch, tmp_path):
    """Formato do Meta Graph API — hoje inerte (só a Evolution chama esse /webhook),
    mantido por clareza/compatibilidade futura."""
    main = carregar_app(monkeypatch, tmp_path)

    payload = {"message": {"interactive": {"type": "button_reply", "button_reply": {"id": "menu", "title": "Menu"}}}}
    texto, id_interacao = main.extrair_conteudo(payload)

    assert texto == "Menu"
    assert id_interacao == "menu"


def test_extrair_conteudo_tipo_nao_suportado_retorna_none(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    texto, id_interacao = main.extrair_conteudo({"message": {"audioMessage": {}}})

    assert texto is None
    assert id_interacao is None


# --- connection.update: estado da instância refletido de verdade, não inventado ----


def _seed_empresa_com_instancia(main, slug: str, instancia: str):
    models = importlib.import_module("core.models")
    db = main.SessionLocal()
    try:
        empresa = models.Empresa(
            nome="Clínica A",
            slug=slug,
            segmento="clinica",
            telefone_whatsapp="5586999999950",
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


def test_connection_update_persiste_o_estado_real_da_instancia(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    empresa = _seed_empresa_com_instancia(main, "clinica-a", "instancia-a")

    payload = {
        "event": "connection.update",
        "instance": "instancia-a",
        "data": {"instance": "instancia-a", "state": "open"},
    }

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(f"/webhook?token={WEBHOOK_SECRET}", json=payload)

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok"}

    db = main.SessionLocal()
    try:
        models = importlib.import_module("core.models")
        atualizado = db.query(models.Empresa).filter_by(id=empresa.id).first()
    finally:
        db.close()

    assert atualizado.estado_conexao_whatsapp == "open"
    assert atualizado.estado_conexao_atualizado_em is not None


def test_connection_update_nao_processa_como_mensagem(monkeypatch, tmp_path):
    """connection.update não tem 'key'/'message' — não pode cair no parser de texto."""
    main = carregar_app(monkeypatch, tmp_path)
    _seed_empresa_com_instancia(main, "clinica-a", "instancia-a")

    payload = {"event": "connection.update", "instance": "instancia-a", "data": {"instance": "instancia-a", "state": "close"}}

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(f"/webhook?token={WEBHOOK_SECRET}", json=payload)

    assert resposta.status_code == 200


def test_connection_update_de_instancia_desconhecida_nao_quebra(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    payload = {"event": "connection.update", "instance": "nao-existe", "data": {"instance": "nao-existe", "state": "close"}}

    with TestClient(main.app, base_url="https://testserver") as client:
        resposta = client.post(f"/webhook?token={WEBHOOK_SECRET}", json=payload)

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "empresa_nao_encontrada"}
