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
# preencha DATABASE_URL, REDIS_URL, credenciais da Meta, PUBLIC_BASE_URL, ADMIN_PASSWORD e SESSION_SECRET_KEY

python -m scripts.criar_tabelas   # bootstrap idempotente do schema
docker compose up --build -d
```

O healthcheck do container (`/readyz`) só considera o serviço saudável depois de validar a conexão com PostgreSQL e Redis.

`PUBLIC_BASE_URL` precisa ser o domínio público real (o mesmo do Caddy, passo 4) — é ela que a aplicação usa para dizer à Evolution API para onde mandar o webhook ao criar uma instância pelo onboarding ou pelo painel (`/admin/empresas/{id}/conectar`). Se o domínio ainda não estiver resolvendo/com TLS válido quando alguém passar pelo onboarding, a instância é criada mas as mensagens não chegam até o domínio ficar no ar.

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

Ver a lista completa em [bot-app/.env.example](../bot-app/.env.example) e na seção "Variáveis de ambiente" do [README](../README.md). As que impedem a aplicação de subir se ausentes ou fracas: `DATABASE_URL`, `REDIS_URL`, `EVOLUTION_API_KEY`, `PUBLIC_BASE_URL`, `META_TOKEN`, `META_PHONE_NUMBER_ID`, `ADMIN_PASSWORD`, `SESSION_SECRET_KEY`.

> **Atualizando uma instalação existente:** `PUBLIC_BASE_URL` é uma variável nova — se o processo já estava rodando antes dessa mudança, adicione-a ao `.env` antes de reiniciar, senão a aplicação não sobe (falha de validação na inicialização, igual às outras variáveis obrigatórias).

## Template de mensagem para lembretes

A Graph API do WhatsApp só permite mensagem livre dentro da janela de 24h de atendimento ao cliente. Como o lembrete é enviado proativamente, ele precisa de um **template aprovado no Meta Business Manager** — sem esse cadastro, os lembretes falham silenciosamente (erro registrado em log, sem quebrar a aplicação).

Cadastre no Meta Business Manager:

- Nome: `lembrete_agendamento` (ou o valor configurado em `META_TEMPLATE_LEMBRETE_NOME`)
- Categoria: `UTILITY`
- Idioma: Portuguese (BR) — `pt_BR`
- Corpo: `Olá {{1}}! Passando para lembrar do seu horário de {{2}} marcado para {{3}} na {{4}}. Para cancelar, é só responder esta mensagem.`
- Exemplos para submissão: `{{1}}=Maria`, `{{2}}=Corte de cabelo`, `{{3}}=15/08/2026 às 14:00`, `{{4}}=Clínica Sorriso Feliz`

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

- [ ] `ADMIN_PASSWORD` e `SESSION_SECRET_KEY` gerados com valores fortes (não os exemplos do `.env.example`).
- [ ] `PUBLIC_BASE_URL` configurado com o domínio público real e o Caddy já respondendo nele — necessário antes de cadastrar a primeira empresa pelo onboarding.
- [ ] `DATABASE_URL` apontando para PostgreSQL (não SQLite) — o schema é compatível com os dois, mas SQLite não é recomendado para múltiplos workers/produção.
- [ ] Template de lembrete aprovado no Meta Business Manager.
- [ ] Postgres e Redis expostos apenas em `127.0.0.1` (não publicamente).
- [ ] `docker compose up --build` executado após qualquer mudança de código ou de estrutura de pastas — a imagem precisa ser reconstruída.
- [ ] `/readyz` retornando `200` antes de apontar o domínio/tráfego real para o serviço.
- [ ] Backup periódico do PostgreSQL configurado (não incluso neste repositório).

## Bootstrap de schema em bancos já existentes

`core/schema.py` roda automaticamente no startup da aplicação (`ensure_schema()`) e é idempotente: cria tabelas que não existem e adiciona colunas novas em tabelas já existentes, sem exigir um framework de migration. Isso significa que atualizar o código e reiniciar o serviço já aplica mudanças de schema pendentes — não há passo manual de migration a rodar separadamente.
