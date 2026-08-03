import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import preparar_ambiente


def carregar_app(monkeypatch, tmp_path: Path):
    preparar_ambiente(monkeypatch, tmp_path)
    main = importlib.import_module("main")
    main.ensure_schema()
    return main


def test_raiz_redireciona_para_onboarding_quando_nao_existe_empresa(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"


def test_onboarding_rejeita_slug_invalido(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)

    with TestClient(main.app) as client:
        response = client.post(
            "/onboarding",
            data={
                "nome": "Clínica Sorriso Feliz",
                "slug": "Slug Invalido",
                "segmento": "clinica",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "slug" in response.text.lower()


def test_onboarding_cria_empresa_servico_e_sucesso(monkeypatch, tmp_path):
    main = carregar_app(monkeypatch, tmp_path)
    models = importlib.import_module("core.models")

    with TestClient(main.app) as client:
        passo1 = client.post(
            "/onboarding",
            data={
                "nome": "Clínica Sorriso Feliz",
                "slug": "clinica-sorriso-feliz",
                "segmento": "clinica",
            },
            follow_redirects=False,
        )
        assert passo1.status_code == 303
        assert passo1.headers["location"] == "/onboarding/configurar"

        passo2 = client.post(
            "/onboarding/configurar",
            data={
                "telefone_whatsapp": "5586999999999",
                "evolution_instance_name": "clinica-sorriso-feliz",
                "horario_abertura": "08:00",
                "horario_fechamento": "18:00",
                "intervalo_entre_atendimentos_minutos": "15",
                "primeiro_servico_nome": "Consulta inicial",
                "primeiro_servico_duracao_minutos": "30",
                "primeiro_servico_preco": "120,00",
            },
            follow_redirects=False,
        )
        assert passo2.status_code == 303
        assert passo2.headers["location"] == "/onboarding/sucesso"

        sucesso = client.get("/onboarding/sucesso")

    assert sucesso.status_code == 200
    assert "Clínica Sorriso Feliz" in sucesso.text

    db = main.SessionLocal()
    try:
        empresas = db.query(models.Empresa).all()
        servicos = db.query(models.Servico).all()
    finally:
        db.close()

    assert len(empresas) == 1
    assert len(servicos) == 1
    assert empresas[0].nome == "Clínica Sorriso Feliz"
    assert servicos[0].nome == "Consulta inicial"
