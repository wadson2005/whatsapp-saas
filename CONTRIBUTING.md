# Contribuindo

Obrigado por considerar contribuir com este projeto. Este guia cobre o fluxo esperado, os padrões de código já usados no repositório e como validar uma mudança antes de abrir um PR.

## Fluxo de contribuição

1. Abra uma issue descrevendo o problema ou a funcionalidade antes de começar a implementar, especialmente para mudanças de comportamento ou de schema.
2. Crie um branch a partir de `master`: `git checkout -b feature/nome-da-mudanca`.
3. Implemente a mudança com testes cobrindo o caso novo (ou o bug corrigido).
4. Rode a suíte completa localmente (`python -m pytest`) antes de abrir o PR.
5. Abra o Pull Request descrevendo o quê e o porquê da mudança — o "quê" geralmente já está no diff, o "porquê" é o que realmente importa na revisão.

## Padrões de código

- **Domínio em português, padrões de linguagem em inglês.** Nomes de funções, variáveis e mensagens de negócio seguem o vocabulário do domínio (`agendar_servico`, `buscar_resposta`, `empresa_id`) porque é assim que o time e os stakeholders do produto falam sobre o sistema. Convenções técnicas gerais (nomes de exceções built-in, type hints, etc.) seguem o padrão da linguagem.
- **Sem duplicação.** Antes de escrever uma função nova, verifique se algo parecido já existe em `core/`, `services/` ou `ai/`.
- **Sem abstrações antecipadas.** Não crie interfaces, classes base ou camadas genéricas para um único caso de uso. Três linhas repetidas são melhores que uma abstração prematura.
- **Tratamento de erros explícito.** Erros esperados (validação de formulário, conflito de agendamento) retornam valores/estruturas de erro; falhas de infraestrutura (rede, banco) são logadas com `logging` e nunca derrubam o fluxo principal quando existe um fallback razoável (ver `ai/service.py` e `integrations/meta_client.py` como referência).
- **Sem `print()` para depuração.** Use o `logger` do módulo (`logging.getLogger(__name__)`) com o nível apropriado (`debug`, `info`, `warning`, `exception`).
- **Compatibilidade SQLite/PostgreSQL.** Qualquer SQL cru (fora do ORM) precisa funcionar nos dois bancos — literais booleanos, por exemplo, passam por `core/db_compat.sql_bool()`. Não assuma sintaxe específica de um dialeto.

## Estrutura para novo código

- Lógica de negócio nova → `services/`.
- Cliente de uma API externa nova → `integrations/`.
- Configuração, modelo de dados ou infraestrutura compartilhada → `core/`.
- Rota nova do painel → `admin.py`, sempre com `Depends(get_db)` e `Depends(require_admin)`.
- Rota pública nova → `main.py`.
- Script de linha de comando → `scripts/`, executado com `python -m scripts.nome_do_script`.

## Testes

```bash
cd bot-app
python -m pytest
```

- Os testes usam SQLite em arquivo temporário (`tmp_path`) e simulam Redis, Meta Graph API e o provider de IA — nenhum teste depende de um serviço externo real.
- `tests/conftest.py` centraliza o ambiente de bootstrap (variáveis de ambiente, recarregamento de módulos, `FakeRedis`). Reaproveite `preparar_ambiente()` em vez de duplicar esse setup em um teste novo.
- Ao adicionar um módulo novo em `core/`, `services/`, `integrations/` ou `ai/`, inclua o nome dele em `APP_MODULES` (`tests/conftest.py`) para que o cache de import seja limpo entre testes.

## Banco de dados

Mudanças no schema (novas colunas, novas tabelas) devem:

1. Ser declaradas em `core/models.py` (fonte da verdade para `Base.metadata.create_all()`).
2. Ter uma entrada correspondente em `core/schema.py` (`_add_column_if_missing`) para bancos já existentes em produção, usando `core/db_compat.py` para qualquer literal que difira entre SQLite e PostgreSQL.
3. Ser validadas rodando a suíte de testes (que recria o schema do zero) e, idealmente, testando manualmente contra um PostgreSQL local ou em container.

## Dúvidas

Abra uma issue — preferimos alinhar a abordagem antes da implementação do que revisar um PR grande com uma direção diferente da esperada.
