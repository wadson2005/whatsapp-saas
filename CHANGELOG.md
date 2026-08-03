# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## [Unreleased]

### Alterado

- Reorganizado `bot-app/` em pacotes por responsabilidade: `core/` (config, banco, modelos, schema, redis), `services/` (regras de negócio), `integrations/` (clientes de APIs externas) e `scripts/` (utilitários de linha de comando).
- Rotas de `main.py` e `admin.py` migradas para injeção de dependência do FastAPI (`Depends(get_db)`, `Depends(require_admin)`), eliminando abertura/fechamento manual de sessão em cada rota.
- `integrations/meta_client.py` centraliza o envio HTTP para a Graph API em um único helper com tratamento de falha de rede, em vez de três blocos quase idênticos.
- Comparação de senha do painel administrativo passou a usar `hmac.compare_digest` (evita timing attack).
- Duplicação eliminada em `services/agenda.py` (`_parse_lista_dias` era idêntica a `_parse_lista_inteiros`).
- Suíte de testes consolidada em `tests/conftest.py` (ambiente de bootstrap, lista de módulos recarregados e `FakeRedis` deixaram de estar duplicados em 10 arquivos de teste diferentes).
- Documentação reorganizada: `README.md` reescrito como porta de entrada; conteúdo técnico aprofundado movido para `docs/architecture.md` e `docs/deployment.md`.

### Corrigido

- Bootstrap de schema (`core/schema.py`) usava `BOOLEAN DEFAULT 1` em uma migration manual — sintaxe aceita pelo SQLite mas rejeitada pelo PostgreSQL. Corrigido para `TRUE`/`FALSE` via `core/db_compat.sql_bool()`, com verificação real contra um PostgreSQL em container.
- `print()` de depuração no caminho do webhook e do cliente da Meta substituídos por `logging` (nível `debug`), evitando saída não controlada em produção.
- Scripts `scripts/pausar.py` e `scripts/retomar.py` não tratavam o caso de a empresa de seed não existir (erro não tratado); agora emitem uma mensagem clara e saem com código de erro.

### Removido

- `bot-app/database.py.bak` (arquivo morto versionado por engano).
- `evolution/conversa.py` e `evolution/botoes.json` / `botoes_meta.json` — protótipo superado pela máquina de estados atual em `bot-app/conversa.py`; os JSONs continham um número de telefone real hardcoded.
- Diretório `.claude/` (configuração local de ferramentas de desenvolvimento) removido do controle de versão.

### Segurança

- Domínio de produção real removido do `Caddyfile` versionado (substituído por placeholder); caminho/usuário reais removidos de `bot-app.service`.

## [0.5.0] - 2026-08-03

### Adicionado

- Configurações operacionais (Meta, IA, lembretes, palavra de ativação) editáveis em `/admin/configuracoes`, com efeito imediato sem reiniciar o processo.

### Corrigido

- Correções gerais de bugs no painel e no fluxo de configuração.

## [0.4.0] - 2026-08-03

### Adicionado

- Painel de clientes (busca, ordenação, ficha de detalhe).
- Lembretes automáticos de agendamento via template da Meta.
- Camada de IA (NLU) opcional como fallback da máquina de estados.
- Base de conhecimento por empresa e página de insights automáticos.

## [0.3.0] - 2026-07-31

### Adicionado

- Fluxo de atendimento humano com fila de solicitações e mudança de status.
- Painel funcional com cadastro de empresas, serviços e clientes.
- Integração completa com a Meta Graph API para mensagens interativas.

## [0.2.0] - 2026-07-25

### Adicionado

- `bot-app.service` (systemd) para supervisão do processo em produção.

### Corrigido

- Rede Docker (`host.docker.internal`) e restrição de portas expostas publicamente.

## [0.1.0] - 2026-07-25

### Adicionado

- Primeira versão: infraestrutura da Evolution API e bot com banco de dados multi-tenant.
