# WhatsApp SaaS — Plataforma de Agendamento Multi-Tenant via WhatsApp

Plataforma de atendimento e agendamento via WhatsApp para pequenos negócios (clínicas, barbearias, salões, restaurantes), com um único código-base atendendo várias empresas ao mesmo tempo. Cada empresa tem seu próprio catálogo de serviços, horários, base de conhecimento e configurações — isolados por `empresa_id` no mesmo banco.

[![Testes](https://img.shields.io/badge/testes-120%20passing-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.12-blue)]()
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)]()
[![Licença](https://img.shields.io/badge/licença-MIT-blue)](LICENSE)

## Visão geral

O sistema recebe mensagens do WhatsApp via webhook, conduz o cliente por uma máquina de estados (escolha de serviço, horário e confirmação), grava o agendamento em PostgreSQL com validação real de conflito e disponibilidade, e devolve a resposta como mensagem interativa (botões ou lista) pela Meta Graph API. Um painel administrativo web cobre toda a operação sem precisar de terminal: cadastro de empresas e serviços, agenda, base de conhecimento, métricas, insights e configurações operacionais.

```
WhatsApp → Evolution API (webhook) → FastAPI → máquina de estados → PostgreSQL
                                                       │
                                          base de conhecimento (determinística)
                                                       │
                                        camada de IA opcional (fallback)
                                                       │
                                    Meta Graph API → resposta interativa
```

Documentação mais profunda:

- [docs/architecture.md](docs/architecture.md) — fluxo de conversa, camada de IA, base de conhecimento, modelo de dados, decisões de design.
- [docs/deployment.md](docs/deployment.md) — Docker, systemd, Caddy, variáveis obrigatórias, checklist de produção.

## Principais funcionalidades

- **Onboarding público** (`/onboarding`) — cadastra empresa, primeiro serviço, cria o usuário `admin` da empresa (já autenticado no painel ao final) e cria/conecta a instância do WhatsApp automaticamente (QR code na hora), sem terminal, sem tocar na Evolution API manualmente e sem depender de cadastro manual de usuário depois.
- **Motor de agendamento** — valida empresa/serviço ativos, horário de funcionamento, almoço, dias indisponíveis e conflito com outros agendamentos; sugere horários alternativos automaticamente.
- **Máquina de estados conversacional** — fluxo guiado por botões/listas interativas, com atalhos globais (`menu`, `cancelar`, `reagendar`) e fallback contextual (nunca deixa o cliente sem resposta).
- **Lembretes automáticos** — ciclo assíncrono embutido no processo, envia lembrete via template aprovado da Meta antes do horário marcado, com controle de envio único por agendamento.
- **Base de conhecimento por empresa** — perguntas e respostas cadastradas via painel, consultadas antes de qualquer chamada de IA (garante que uma resposta cadastrada nunca é substituída por algo inventado).
- **Camada de IA opcional (NLU)** — interpretação de linguagem natural como último recurso da máquina de estados, com provider plugável (OpenAI hoje, interface pronta para outros), cache Redis, timeout e fallback seguro. Desligada por padrão — zero impacto se não configurada.
- **Atendimento humano** — registra e organiza solicitações de handoff, com fila e mudança de status no painel.
- **Painel administrativo completo** — dashboard com indicadores por período, insights automáticos gerados a partir de dados reais, listagem de clientes inativos, CRUD de empresas/serviços/conhecimento, e configurações operacionais editáveis sem reiniciar o processo.
- **Multi-tenant real, inclusive no acesso** — isolamento por `empresa_id` em todas as tabelas de negócio; cada empresa cliente pode logar com seu próprio usuário e só enxerga os próprios dados, sem depender do acesso único da plataforma.
- **Papéis e permissões** — usuário `admin` de uma empresa gerencia serviços, conhecimento e outros usuários da própria empresa; `operador` cobre o dia a dia (agenda, clientes, atendimento) sem acesso a exclusões, configurações ou gestão de usuários.
- **Recuperação de senha self-service** (`/admin/esqueci-senha`) — link de redefinição por e-mail, válido por 1 hora e de uso único, sem depender de um superadmin para redefinir a senha de outro usuário.
- **Conexão de WhatsApp sem intervenção manual na Evolution API** — criar a empresa já cria a instância e configura o webhook; se a sessão do WhatsApp cair depois (celular trocado, dispositivo desvinculado), `/admin/empresas/{id}/conectar` gera um novo QR code direto do painel.

## Diferenciais técnicos

- **Compatibilidade real SQLite/PostgreSQL** — os testes rodam em SQLite e a produção em PostgreSQL contra o mesmo bootstrap de schema, sem duplicar lógica de migration (ver `core/db_compat.py`).
- **Configuração operacional viva no banco** — token da Meta, provider de IA, antecedência de lembrete e palavra de ativação são editáveis pelo painel e valem imediatamente, sem redeploy.
- **IA como fallback, nunca como autoridade** — a camada de IA só é consultada quando a máquina de estados e a base de conhecimento não resolvem, e nunca executa uma ação destrutiva (cancelamento, por exemplo) sem passar pela tela de confirmação normal.
- **Suíte de testes de verdade** — 120 testes automatizados cobrindo onboarding, conversa, agendamento, lembretes, base de conhecimento, métricas, papéis/permissões e a camada de IA (com providers e Redis simulados, sem depender de serviços externos).
- **Bootstrap de schema idempotente** — cria tabelas novas e adiciona colunas em bancos já existentes automaticamente, sem exigir um framework de migration para um projeto deste porte.

## Stack técnica

| Camada | Tecnologia | Papel |
|---|---|---|
| API / painel | Python 3.12 + FastAPI + Uvicorn | Webhook, painel administrativo, onboarding |
| Persistência | PostgreSQL (produção) / SQLite (testes) + SQLAlchemy 2.0 | Empresas, serviços, clientes, agendamentos |
| Estado de conversa | Redis | Passo atual da conversa, com expiração automática |
| Integração WhatsApp | Evolution API | Recebe os webhooks de mensagem |
| Mensagens interativas | Meta Graph API | Envia botões, listas e templates |
| Camada de IA (opcional) | OpenAI (interface plugável) | Interpretação de linguagem natural como fallback |
| Templates | Jinja2 + Bootstrap | Painel administrativo server-side |
| Infraestrutura | Docker + Docker Compose + systemd + Caddy | Build, orquestração, supervisão e proxy reverso |
| Testes | pytest + FastAPI TestClient | 101 testes, banco SQLite isolado por teste |

## Arquitetura de código

O backend (`bot-app/`) é organizado em camadas:

- **`core/`** — infraestrutura compartilhada: configuração (`config.py`), conexão com o banco (`database.py`), modelos SQLAlchemy (`models.py`), bootstrap de schema (`schema.py`), compatibilidade SQLite/PostgreSQL (`db_compat.py`), cliente Redis (`redis_client.py`) e hashing de senha (`security.py`).
- **`services/`** — regras de negócio: motor de agendamento (`agenda.py`), base de conhecimento (`conhecimento.py`), atendimento humano (`atendimento_humano.py`), lembretes (`lembretes.py`), métricas/insights (`metricas.py`), configuração operacional (`configuracoes.py`), usuários e papéis do painel (`usuarios.py`) e utilitários de texto (`texto_utils.py`).
- **`integrations/`** — clientes de APIs externas: `meta_client.py` (Meta Graph API) e `evolution_client.py` (criação de instância, QR code de pareamento e status de conexão na Evolution API).
- **`ai/`** — camada de interpretação de linguagem natural, isolada por trás de uma interface de provider única (`provider.py`, `service.py`, `cache.py`, `prompts.py`, `models.py`).
- **`scripts/`** — utilitários de linha de comando: criação de tabelas, seed e pausar/retomar uma empresa.
- **Raiz (`main.py`, `admin.py`, `conversa.py`)** — camada de apresentação: entrypoint FastAPI, sub-aplicação do painel administrativo e a máquina de estados da conversa. Ambas as rotas usam injeção de dependência do FastAPI (`Depends(get_db)`) para a sessão do banco, sem sessões manuais espalhadas pelas rotas.

## Instalação e execução local

### Pré-requisitos

- Python 3.12+
- PostgreSQL 16 (ou SQLite para testar rapidamente sem subir banco)
- Redis 7
- Uma instância da [Evolution API](https://github.com/EvolutionAPI/evolution-api) configurada e um número de teste na [Meta Graph API](https://developers.facebook.com/docs/whatsapp)

### Passo a passo

```bash
git clone <url-do-repositorio>
cd whatsapp-saas/bot-app

python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# edite o .env com suas credenciais (ver seção "Variáveis de ambiente")

python -m scripts.criar_tabelas   # cria as tabelas (bootstrap idempotente)
python -m scripts.seed            # opcional: cria uma empresa de exemplo

uvicorn main:app --reload
```

A aplicação sobe em `http://localhost:8000`. O painel fica em `/admin/login` e o onboarding público em `/onboarding`.

### Executando os testes

```bash
cd bot-app
python -m pytest
```

Os testes usam SQLite em arquivo temporário e não dependem de Postgres, Redis ou APIs externas rodando de verdade (todos os clientes externos são simulados).

## Executando via Docker

```bash
cd bot-app
docker compose up --build
```

O `docker-compose.yml` sobe o container do bot com healthcheck na rota `/readyz` (valida conexão com PostgreSQL e Redis antes de considerar o serviço pronto). Para a infraestrutura de apoio (PostgreSQL, Redis e Evolution API), veja `evolution/docker-compose.yml` e o guia completo em [docs/deployment.md](docs/deployment.md).

## Variáveis de ambiente

Todas documentadas em [bot-app/.env.example](bot-app/.env.example). As mais relevantes para subir o projeto:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DATABASE_URL` | sim | String de conexão SQLAlchemy (`postgresql://...` ou `sqlite:///...`) |
| `REDIS_URL` | sim | Conexão com o Redis usado para estado de conversa e cache da IA |
| `EVOLUTION_API_KEY` | sim | Chave da instância da Evolution API |
| `PUBLIC_BASE_URL` | sim | URL pública do bot (ex.: `https://seu-dominio.com`), usada para configurar o webhook ao criar uma instância na Evolution API automaticamente |
| `WEBHOOK_SECRET` | sim | Segredo que autentica `POST /webhook` (vai como `?token=...` na URL configurada na Evolution API; requisições sem o token correto recebem 401) — gere com `openssl rand -hex 32` |
| `META_TOKEN` / `META_PHONE_NUMBER_ID` | sim | Credenciais da Meta Graph API |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | sim | Login do superadmin da plataforma (acesso a todas as empresas) — `ADMIN_PASSWORD` precisa ser forte, valores padrão são rejeitados na inicialização. Usuários por empresa (`admin`/`operador`) são cadastrados depois, em `/admin/usuarios` |
| `SESSION_SECRET_KEY` | sim | Chave de assinatura de sessão (≥16 caracteres, gere com `openssl rand -hex 32`) |
| `AI_ENABLED` | não (padrão `false`) | Liga a camada de IA — sem isso, comportamento idêntico ao de não ter essa camada |
| `SMTP_HOST` / `SMTP_USUARIO` / `SMTP_SENHA` / `SMTP_REMETENTE` | não | Envio do e-mail de "esqueci minha senha" (`/admin/esqueci-senha`). Qualquer provedor com relay SMTP funciona. Sem isso, a tela continua respondendo normalmente, só não envia e-mail nenhum (fica só no log) |

> A maioria das variáveis operacionais (Meta, IA, lembretes, palavra de ativação) só serve como **valor inicial**: depois do primeiro boot, o sistema copia esses valores para a tabela `configuracao_sistema` e passa a usar o banco como fonte viva, editável em `/admin/configuracoes` sem reiniciar o processo.

## Estrutura de diretórios

```
whatsapp-saas/
├── bot-app/                   # aplicação principal (FastAPI)
│   ├── main.py                 # entrypoint: webhook, onboarding, health checks
│   ├── admin.py                 # painel administrativo (sub-app FastAPI)
│   ├── conversa.py             # máquina de estados da conversa
│   ├── core/                   # config, banco, modelos, schema, redis
│   ├── services/                # regras de negócio (agenda, lembretes, métricas...)
│   ├── integrations/            # clientes de APIs externas (Meta)
│   ├── ai/                      # camada de interpretação de linguagem natural
│   ├── scripts/                  # CLI: criar tabelas, seed, pausar/retomar
│   ├── templates/                # HTML do painel e do onboarding (Jinja2)
│   ├── tests/                    # suíte de testes (pytest)
│   ├── Dockerfile / docker-compose.yml
│   └── requirements.txt
├── evolution/                  # infraestrutura de apoio (Evolution API, Postgres, Redis)
├── docs/                       # documentação técnica aprofundada
├── Caddyfile                   # proxy reverso de produção
└── bot-app.service             # unit file systemd (template)
```

## Endpoints principais

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/webhook` | Recebe eventos da Evolution API e processa a mensagem |
| `GET` | `/onboarding` | Wizard público de cadastro de empresa (cria e conecta o WhatsApp automaticamente) |
| `GET` | `/onboarding/conectar` | QR code / código de pareamento para conectar o WhatsApp da empresa recém-criada |
| `GET` | `/healthz` | Liveness check |
| `GET` | `/readyz` | Readiness check — valida PostgreSQL e Redis |
| `GET` | `/admin/login` | Login do painel administrativo |
| `GET` | `/admin/dashboard` | Métricas e indicadores por período |
| `GET` | `/admin/empresas`, `/admin/servicos`, `/admin/clientes` | CRUDs administrativos |
| `GET` | `/admin/empresas/{id}/conectar` | Gera um novo QR code para reconectar o WhatsApp de uma empresa já existente |
| `GET` | `/admin/conhecimento` | Base de conhecimento por empresa |
| `GET` | `/admin/usuarios` | Gestão de usuários e papéis (admin/operador) por empresa |
| `GET` | `/admin/insights`, `/admin/clientes-inativos` | Insights automáticos e clientes inativos |
| `GET`/`POST` | `/admin/configuracoes` | Configuração operacional (Meta, IA, lembretes) — restrita ao superadmin |

## Exemplo de uso

Payload típico recebido em `/webhook` quando um cliente envia uma mensagem de texto:

```json
{
  "instance": "clinica-sorriso-feliz",
  "data": {
    "key": { "fromMe": false, "remoteJid": "5586999999999@s.whatsapp.net" },
    "message": { "conversation": "oibot" }
  }
}
```

O bot identifica a empresa pela `instance`, abre (ou recupera) o estado da conversa no Redis e responde com a lista de serviços ativos daquela empresa via Meta Graph API.

## Roadmap

- [x] Papéis e permissões no painel administrativo — login por empresa (`admin`/`operador`), superadmin de plataforma via `.env` como bootstrap.
- [x] Cadastro manual de cliente final direto pelo painel.
- [x] Criação do primeiro usuário da empresa integrada ao onboarding público — o onboarding já cria o usuário `admin` da empresa e autentica no painel automaticamente, sem depender de cadastro manual.
- [x] Recuperação de senha self-service (`/admin/esqueci-senha`) via e-mail (SMTP genérico).
- [ ] Cobrança recorrente (assinatura por empresa).
- [ ] Observabilidade e auditoria mais completas (métricas operacionais, alertas).
- [ ] Ampliar os pontos de fallback cobertos pela camada de IA e adicionar mais providers.
- [ ] Criptografia em repouso para segredos guardados no banco (token da Meta, chave de IA).
- [ ] Distribuição automática de solicitações de atendimento humano para operadores.

## Melhorias futuras possíveis

- Busca semântica (embeddings) na base de conhecimento, se o volume de perguntas justificar.
- Campanhas de retorno automatizadas a partir da listagem de clientes inativos.
- Renovação/validação automática de token da Meta.
- Cache/reuso de cliente HTTP para as chamadas à Meta Graph API.

## Contribuindo

Veja [CONTRIBUTING.md](CONTRIBUTING.md) para o fluxo de contribuição, padrões de código e como rodar a suíte de testes.

## Sobre o desenvolvimento deste projeto

Este projeto foi desenvolvido com apoio de ferramentas de IA (Claude) como assistente de codificação. A IA foi usada para acelerar a escrita de código e sugerir implementações; cada sugestão foi pesquisada e validada por mim antes de ser aceita, da mesma forma como eu usaria documentação oficial ou Stack Overflow — como parte do processo de aprendizado, não como substituto do entendimento técnico. Entendo a arquitetura, as decisões de design e o funcionamento do código deste repositório, e estou apto a explicá-lo e estendê-lo.

## Licença

Distribuído sob a licença MIT. Veja [LICENSE](LICENSE) para o texto completo.
