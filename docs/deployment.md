# Deploy em produção

Guia de referência para colocar o projeto no ar em uma VPS Linux. Ajuste caminhos, usuário e domínio para o seu ambiente — os valores usados aqui (`/opt/whatsapp-saas`, usuário `deploy`, `your-domain.example.com`) são placeholders.

## Visão geral da infraestrutura

```
Internet → Caddy (proxy reverso, TLS automático) → bot-app (Docker, porta 8000)
                                                          │
                                              PostgreSQL + Redis + Evolution API
                                              (evolution/docker-compose.yml)
```

- `evolution/docker-compose.yml` sobe PostgreSQL 16 (`127.0.0.1:5432`), Redis 7 (`127.0.0.1:6379`) e a Evolution API (`127.0.0.1:8080`) — nenhum desses serviços fica exposto publicamente.
- `bot-app/docker-compose.yml` sobe o container da aplicação em modo `network_mode: host`, escutando na porta 8000.
- O [Caddyfile](../Caddyfile) encaminha o domínio público para a porta local do bot (ou da Evolution API, dependendo de qual endpoint deve ficar público).
- [bot-app.service](../bot-app.service) é um template de unit file systemd que supervisiona o `docker compose up` do bot, reiniciando automaticamente em caso de falha.

## Pré-requisitos no servidor

- Docker + Docker Compose
- systemd (para supervisão do serviço)
- Um domínio apontando para o IP do servidor (para o Caddy emitir certificado TLS automaticamente)

## Passo a passo

### 1. Subir a infraestrutura de apoio

```bash
cd evolution
cp .env.example .env   # preencha POSTGRES_USER, POSTGRES_PASSWORD, EVOLUTION_API_KEY, etc.
docker compose up -d
```

### 2. Configurar e subir o bot

```bash
cd bot-app
cp .env.example .env
# preencha DATABASE_URL, REDIS_URL, credenciais da Meta, PUBLIC_BASE_URL, WEBHOOK_SECRET, ADMIN_PASSWORD e SESSION_SECRET_KEY

python -m scripts.criar_tabelas   # bootstrap idempotente do schema
docker compose up --build -d
```

O healthcheck do container (`/readyz`) só considera o serviço saudável depois de validar a conexão com PostgreSQL e Redis.

`PUBLIC_BASE_URL` precisa ser o domínio público real (o mesmo do Caddy, passo 4) — é ela que a aplicação usa para dizer à Evolution API para onde mandar o webhook ao criar uma instância, seja pelo cadastro de empresa self-service (`/admin/empresas/cadastrar`) ou pelo painel (`/admin/empresas/nova`, `/admin/empresas/{id}/conectar`). Se o domínio ainda não estiver resolvendo/com TLS válido quando alguém cadastrar a empresa, a instância é criada mas as mensagens não chegam até o domínio ficar no ar.

### 3. Supervisionar com systemd

```bash
sudo cp bot-app.service /etc/systemd/system/bot-app.service
# edite User= e WorkingDirectory= no arquivo copiado para refletir o seu servidor
sudo systemctl daemon-reload
sudo systemctl enable --now bot-app
```

### 4. Configurar o proxy reverso

```bash
sudo cp Caddyfile /etc/caddy/Caddyfile
# troque your-domain.example.com pelo domínio real
sudo systemctl reload caddy
```

## Variáveis obrigatórias

Ver a lista completa em [bot-app/.env.example](../bot-app/.env.example) e na seção "Variáveis de ambiente" do [README](../README.md). As que impedem a aplicação de subir se ausentes ou fracas: `DATABASE_URL`, `REDIS_URL`, `EVOLUTION_API_KEY`, `PUBLIC_BASE_URL`, `WEBHOOK_SECRET`, `META_TOKEN`, `META_PHONE_NUMBER_ID`, `ADMIN_PASSWORD`, `SESSION_SECRET_KEY`.

> **Atualizando uma instalação existente:** `PUBLIC_BASE_URL` e `WEBHOOK_SECRET` são variáveis novas — se o processo já estava rodando antes dessa mudança, adicione as duas ao `.env` antes de reiniciar, senão a aplicação não sobe (falha de validação na inicialização, igual às outras variáveis obrigatórias).
>
> **`WEBHOOK_SECRET` especificamente:** `POST /webhook` passou a exigir `?token=<WEBHOOK_SECRET>` na URL (proteção contra qualquer um forjar mensagens de qualquer empresa). Esse token só é embutido na URL do webhook no momento em que a instância é **criada** na Evolution API (onboarding ou "Nova empresa" no painel). Empresas cadastradas **antes** dessa mudança têm o webhook configurado na Evolution API com a URL antiga, sem token — depois do deploy, o bot vai parar de responder para elas (toda mensagem cai em 401) até o webhook dessa instância ser reconfigurado manualmente na Evolution API (Manager UI ou API) apontando para `PUBLIC_BASE_URL/webhook?token=<WEBHOOK_SECRET>`.

## Lembretes de agendamento

Cada empresa ativa em `/admin/configurar-bot/lembretes` se quer lembrete por e-mail — ver [docs/architecture.md](architecture.md#lembretes-de-agendamento-e-mail). Basta configurar `RESEND_API_KEY`/`EMAIL_FROM_ENDERECO` (ver [Resend](https://resend.com)) — não exige nenhum cadastro prévio de template.

> O canal WhatsApp (que exigia um template aprovado no Meta Business Manager) foi removido temporariamente — ver `docs/architecture.md`.

## Comandos úteis

```bash
# Status dos containers de infraestrutura
cd evolution && docker compose ps

# Logs da Evolution API
cd evolution && docker compose logs evolution-api --tail=80

# Logs do bot
cd bot-app && docker compose logs -f

# Reiniciar o bot após alterar código
sudo systemctl restart bot-app

# Status do serviço
sudo systemctl status bot-app

# Pausar ou retomar a empresa de teste (scripts/seed.py)
cd bot-app && python -m scripts.pausar
cd bot-app && python -m scripts.retomar
```

## Checklist antes de ir para produção

- [ ] `ADMIN_PASSWORD`, `SESSION_SECRET_KEY` e `WEBHOOK_SECRET` gerados com valores fortes (não os exemplos do `.env.example`).
- [ ] `PUBLIC_BASE_URL` configurado com o domínio público real e o Caddy já respondendo nele — necessário antes de cadastrar a primeira empresa (`/admin/empresas/cadastrar` ou `/admin/empresas/nova`).
- [ ] `DATABASE_URL` apontando para PostgreSQL (não SQLite) — o schema é compatível com os dois, mas SQLite não é recomendado para múltiplos workers/produção.
- [ ] `RESEND_API_KEY`/`EMAIL_FROM_ENDERECO` configurados, se for usar lembrete por e-mail.
- [ ] Postgres e Redis expostos apenas em `127.0.0.1` (não publicamente).
- [ ] `docker compose up --build` executado após qualquer mudança de código ou de estrutura de pastas — a imagem precisa ser reconstruída.
- [ ] `/readyz` retornando `200` antes de apontar o domínio/tráfego real para o serviço.
- [ ] Backup periódico do PostgreSQL configurado (não incluso neste repositório).

## Bootstrap de schema em bancos já existentes

`core/schema.py` roda automaticamente no startup da aplicação (`ensure_schema()`) e é idempotente: cria tabelas que não existem e adiciona colunas novas em tabelas já existentes, sem exigir um framework de migration. Isso significa que atualizar o código e reiniciar o serviço já aplica mudanças de schema pendentes — não há passo manual de migration a rodar separadamente.
