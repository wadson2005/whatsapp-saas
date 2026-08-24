from services.texto_utils import gerar_slug


def test_gerar_slug_remove_acentos_e_espacos():
    assert gerar_slug("Salão Aurora") == "salao-aurora"


def test_gerar_slug_remove_simbolos():
    assert gerar_slug("Barbearia do Zé & Filhos!!") == "barbearia-do-ze-filhos"


def test_gerar_slug_colapsa_hifens_repetidos():
    assert gerar_slug("Café   com --- Leite") == "cafe-com-leite"


def test_gerar_slug_ja_valido_permanece_igual():
    assert gerar_slug("clinica-sorriso-feliz") == "clinica-sorriso-feliz"


def test_gerar_slug_string_vazia():
    assert gerar_slug("") == ""
