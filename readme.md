# WhatsApp SaaS - Bot de Agendamento Multi-Tenant

Sistema de automação de atendimento via WhatsApp pensado para atender várias empresas com o mesmo código-base. A ideia central é simples: cada cliente é um registro no banco, e a lógica do bot decide o fluxo da conversa a partir da empresa que recebeu a mensagem.

> Status atual: plataforma funcional em evolução. O fluxo principal de atendimento, o painel administrativo e o motor de agendamento já estão operacionais e validados em runtime.

## Visão geral

O projeto está dividido em duas partes principais:

- `evolution/`, que sobe a infraestrutura de WhatsApp, banco e cache.
- `bot-app/`, que recebe o webhook, interpreta a conversa e grava os dados de negócio.

O caminho de execução hoje é este:

1. O WhatsApp chega na Evolution API.
2. A Evolution API dispara um webhook para o FastAPI em `bot-app/main.py`.
3. O bot identifica a instância, procura a empresa correspondente e verifica se ela está ativa.
4. A conversa continua em Redis, com estado por telefone e expiração automática.
5. O agendamento é gravado no PostgreSQL com regras reais de disponibilidade, conflito, reagendamento e cancelamento.
6. A resposta volta por texto simples via Evolution API ou por mensagens interativas via Meta Graph API.

## Stack técnica

| Camada | Tecnologia | Papel |
|---|---|---|
| API do bot | Python + FastAPI + Uvicorn | Recebe webhooks e orquestra o fluxo |
| Persistência | PostgreSQL + SQLAlchemy | Guarda empresas, serviços, clientes e agendamentos |
| Estado de conversa | Redis | Mantém o passo atual da conversa por 30 minutos |
| Integração WhatsApp | Evolution API | Recebe mensagens e envia textos simples |
| Mensagens interativas | Meta Graph API | Envia botões e listas, contornando limitações observadas na Evolution API |
| Infraestrutura | Docker + Docker Compose | Sobe Postgres, Redis e Evolution API |
| Execução do bot | Docker + systemd | Mantém o `bot-app` ativo na VPS |
| Saúde do serviço | `/healthz` e `/readyz` | Permitem checagem simples e validação de banco/Redis |
| Painel administrativo | FastAPI + Jinja2 + Bootstrap | Interface web para operação sem terminal |
| Proxy reverso | Caddy | Encaminha o domínio público para a aplicação |

## Estrutura do projeto

```
/home/wadson/stack/
├── Caddyfile
├── bot-app.service
├── readme.md
├── bot-app/
│   ├── .dockerignore
│   ├── .env
│   ├── .env.example
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── config.py
│   ├── admin.py
│   ├── requirements.txt
│   ├── conversa.py
│   ├── criar_tabelas.py
│   ├── database.py
│   ├── main.py
│   ├── meta_client.py
│   ├── models.py
│   ├── pausar.py
│   ├── redis_client.py
│   ├── retomar.py
│   ├── templates/
│   │   └── admin/
│   │       ├── agendamentos_list.html
│   │       ├── base.html
│   │       ├── dashboard.html
│   │       ├── empresa_form.html
│   │       ├── empresas_list.html
│   │       ├── login.html
│   │       ├── servico_form.html
│   │       └── servicos_list.html
│   └── seed.py
└── evolution/
    ├── botoes.json
    ├── botoes_meta.json
    ├── conversa.py
    ├── docker-compose.yml
    └── init-db.sh
```

### O que cada parte faz

- [bot-app/config.py](/home/wadson/stack/bot-app/config.py) centraliza a configuração da aplicação com Pydantic Settings.
- [bot-app/admin.py](/home/wadson/stack/bot-app/admin.py) implementa login, dashboard e CRUD administrativo.
- [bot-app/main.py](/home/wadson/stack/bot-app/main.py) recebe o webhook e dispara o processamento da mensagem.
- [bot-app/conversa.py](/home/wadson/stack/bot-app/conversa.py) contém a máquina de estados da conversa.
- [bot-app/models.py](/home/wadson/stack/bot-app/models.py) define o modelo de dados principal.
- [bot-app/meta_client.py](/home/wadson/stack/bot-app/meta_client.py) envia botões e listas pela API oficial da Meta.
- [bot-app/database.py](/home/wadson/stack/bot-app/database.py) e [bot-app/redis_client.py](/home/wadson/stack/bot-app/redis_client.py) centralizam conexões.
- [evolution/docker-compose.yml](/home/wadson/stack/evolution/docker-compose.yml) sobe a infraestrutura de apoio.
- [Caddyfile](/home/wadson/stack/Caddyfile) faz o reverse proxy para a porta local do bot.

## Modelo de dados

O banco trabalha com quatro tabelas principais:

- `empresas`: identifica cada cliente atendido pelo bot.
- `servicos`: serviços disponíveis por empresa.
- `clientes_finais`: pessoas que conversaram com o bot.
- `agendamentos`: registros criados ao final do fluxo.

O isolamento é multi-tenant por coluna `empresa_id`, então uma mesma base atende várias empresas sem misturar os dados.

## Fluxo de conversa

O fluxo hoje é uma máquina de estados simples, sem IA generativa nem NLP avançado.

- Estado inicial: ignora mensagens aleatórias e só abre conversa quando a mensagem bate com a palavra de ativação `oibot` ou quando o usuário toca em uma ação prevista.
- Lista de serviços: busca os serviços ativos da empresa e envia uma lista interativa.
- Escolha do período: apresenta botões para manhã, tarde ou texto livre.
- Confirmação: cria cliente e agendamento, salva no banco e limpa o estado do Redis.

Também existem atalhos de controle como `menu`, `voltar` e `cancelar`, que reiniciam a conversa.

## Estado atual

O que já existe e funciona hoje:

- Configuração centralizada em [bot-app/config.py](/home/wadson/stack/bot-app/config.py).
- Arquivo de exemplo de ambiente em [bot-app/.env.example](/home/wadson/stack/bot-app/.env.example).
- Dependências fixadas em [bot-app/requirements.txt](/home/wadson/stack/bot-app/requirements.txt).
- Bot containerizado com [bot-app/Dockerfile](/home/wadson/stack/bot-app/Dockerfile) e [bot-app/docker-compose.yml](/home/wadson/stack/bot-app/docker-compose.yml).
- Rotas de saúde `/healthz` e `/readyz` ativas no FastAPI.
- Painel administrativo com login, dashboard, cadastro de empresas, cadastro de serviços e listagem de agendamentos.
- Recebimento de webhooks da Evolution API.
- Identificação da empresa pela instância recebida no payload.
- Máquina de estados com Redis e expiração de 30 minutos.
- Cadastro de empresa, serviços e cliente final.
- Registro de agendamento no banco com validação de janela, conflito e horário de funcionamento.
- Reagendamento e cancelamento operacionais no fluxo do bot.
- Envio de mensagens interativas pela Meta Graph API.
- Script de seed com uma empresa de teste: `Clínica Sorriso Feliz`.
- Scripts simples para pausar e retomar a empresa de teste.
- Bootstrap automático de schema para ambientes já existentes.

O que ainda está faltando para virar produto de fato:

- Multi-tenant mais robusto, com papéis de usuário e permissões no painel.
- Cadastro e manutenção de clientes sem terminal.
- Fluxo de cobrança.
- Observabilidade e auditoria mais completas.
- Testes automatizados.

## Limitações e bugs conhecidos

Esses pontos aparecem no código atual e devem ser tratados como limitações reais, não como detalhe de documentação.

- O fluxo ainda depende de regras explícitas e botões/listas; não há NLP nem IA generativa no entendimento de mensagem.
- O reconhecimento de botões/listas ainda depende bastante do texto exibido; o `id` da interação existe no webhook, mas não é usado em todos os caminhos.
- Quando a empresa não tem serviços ativos, a função que monta a lista simplesmente retorna sem resposta visível para o usuário.

Algumas melhorias já foram concluídas e por isso não aparecem mais como risco:

- As credenciais e URLs do bot já foram removidas do código e passaram para [bot-app/config.py](/home/wadson/stack/bot-app/config.py) e [bot-app/.env](/home/wadson/stack/bot-app/.env).
- O bot já roda em container e é supervisionado por systemd via compose.
- A instalação agora é reproduzível com [bot-app/requirements.txt](/home/wadson/stack/bot-app/requirements.txt).
- O painel administrativo já está disponível em `/admin`.
- O motor de agenda já foi validado com criação, conflito, reagendamento e cancelamento em base real.

## Infraestrutura e deploy

O `docker-compose.yml` sobe:

- PostgreSQL 16, com porta local em `127.0.0.1:5432`.
- Redis 7, com porta local em `127.0.0.1:6379`.
- Evolution API em `127.0.0.1:8080`.

O bot roda em container, via [bot-app/docker-compose.yml](/home/wadson/stack/bot-app/docker-compose.yml), e o systemd apenas supervisiona esse compose através de [bot-app.service](/home/wadson/stack/bot-app.service).

O Caddy encaminha o domínio público para `localhost:8080`, enquanto o bot permanece exposto na porta 8000 localmente.

O container do bot também publica healthcheck para a rota `/readyz`, que valida conexão com PostgreSQL e Redis antes de ser considerado pronto.

O painel administrativo usa as mesmas credenciais definidas em [bot-app/.env.example](/home/wadson/stack/bot-app/.env.example), por meio das variáveis `ADMIN_USERNAME`, `ADMIN_PASSWORD` e `SESSION_SECRET_KEY`.

## Segurança

Algumas decisões já foram tomadas corretamente:

- Bancos e Redis não ficam abertos para a internet.
- O reverse proxy centraliza a entrada pública.
- O estado da conversa expira automaticamente.

Mas ainda existem pontos a melhorar:

- Tirar segredos do código-fonte.
- Adicionar autenticação ao futuro painel administrativo.
- Validar e renovar tokens da Meta com processo controlado.
- Rever logs, alertas e política de backup.

O primeiro item já foi parcialmente atendido: as credenciais da aplicação saíram do código e foram centralizadas em ambiente, mas o processo de rotação e gestão segura de segredos ainda precisa de formalização.

## Melhorias futuras

Se o objetivo for transformar isso em um produto vendável, os próximos passos mais úteis são:

1. Fortalecer permissões e papéis no painel administrativo.
2. Cobrir o fluxo com testes automatizados.
3. Formalizar onboarding de novos clientes.
4. Acrescentar cobrança recorrente e notificações operacionais.
5. Melhorar observabilidade com métricas e alertas operacionais.

## Comandos úteis

```bash
# Ver os containers da infraestrutura
cd /home/wadson/stack/evolution && docker compose ps

# Ver logs da Evolution API
cd /home/wadson/stack/evolution && docker compose logs evolution-api --tail=80

# Reiniciar o bot após alterar código
sudo systemctl restart bot-app

# Ver o status do serviço
sudo systemctl status bot-app

# Criar tabelas e seed inicial
cd /home/wadson/stack/bot-app && source venv/bin/activate
python criar_tabelas.py
python seed.py

# Pausar ou retomar a empresa de teste
python pausar.py
python retomar.py
```

## Resumo

Este projeto já entrega o esqueleto funcional de um bot de agendamento multi-tenant, com infraestrutura real de produção e fluxo de atendimento válido. O que falta hoje não é a base técnica, mas o fechamento de produto: tratar agenda de verdade, organizar deploy e remover dependências implícitas do ambiente.
