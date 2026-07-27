# WhatsApp SaaS — Bot de Agendamento Multi-Tenant

Sistema de automação de atendimento via WhatsApp, construído para ser vendido como assinatura mensal a pequenos negócios (clínicas, barbearias, restaurantes), reutilizando o mesmo código-base para todos os clientes através de configuração no banco de dados.

> **Status do projeto:** protótipo funcional, validado com WhatsApp real (via Baileys) e com a API oficial da Meta (Cloud API), incluindo mensagens de texto, máquina de estados com memória de conversa, e mensagens interativas com botões. Ainda não está em produção com clientes pagantes.

---

## 1. Visão geral da arquitetura

```
┌──────────────────┐     Webhook      ┌─────────────────────┐
│   WhatsApp          │ ───────────────▶ │   Evolution API        │
│   (Baileys OU        │ ◀─────────────── │   (self-hosted,         │
│   Cloud API oficial) │     Envio        │   Docker)               │
└──────────────────┘                    └──────────┬──────────────┘
                                                     │ Webhook HTTP
                                                     ▼
                                        ┌────────────────────────────┐
                                        │   bot-app (FastAPI)            │
                                        │   - Recebe webhook              │
                                        │   - Identifica a empresa        │
                                        │   - Roda a máquina de estados    │
                                        │   - Responde via Evolution API   │
                                        │     OU direto via Meta Graph API │
                                        │     (para botões/listas)          │
                                        └───────┬──────────┬────────────┘
                                                │          │
                                     ┌──────────▼──┐   ┌───▼─────────────┐
                                     │ PostgreSQL     │   │  Redis            │
                                     │ (compartilhado │   │ (estado da         │
                                     │  com a          │   │  conversa,          │
                                     │  Evolution API,  │   │  expira em 30min)   │
                                     │  bancos          │   └───────────────────┘
                                     │  separados)      │
                                     └─────────────────┘
```

**Princípio central do produto:** uma única aplicação (`bot-app`) atende múltiplas empresas (multi-tenant). Cada empresa é uma linha na tabela `empresas`, identificada pelo nome da instância da Evolution API que recebeu a mensagem. Adicionar um novo cliente não exige escrever código novo — só cadastrar a empresa e seus serviços no banco.

---

## 2. Stack técnica

| Camada | Tecnologia | Papel |
|---|---|---|
| Conexão com WhatsApp | Evolution API (self-hosted, Docker) | Abstrai a conexão com o WhatsApp, seja via Baileys (não-oficial) ou Cloud API (oficial da Meta) |
| Backend / lógica de negócio | Python 3.12 + FastAPI + Uvicorn | Recebe webhooks, roda a máquina de estados, decide as respostas |
| Banco de dados persistente | PostgreSQL 16 (via SQLAlchemy) | Dados de empresas, serviços, clientes finais e agendamentos — multi-tenant por coluna `empresa_id` |
| Estado de conversa (memória curta) | Redis 7 | Guarda em que etapa da conversa cada número está, com expiração automática em 30 min |
| Mensagens interativas (botões/listas) | Meta Graph API (chamada direta) | Usado no lugar do endpoint de botões da Evolution API, que apresentou bugs confirmados na versão instalada |
| Infraestrutura | Docker + Docker Compose | Orquestra Postgres, Redis e Evolution API na VPS |
| Processo permanente | systemd (`bot-app.service`) | Mantém o `bot-app` rodando mesmo após reinício da VPS ou queda de conexão SSH |
| Controle de versão | Git + GitHub (repositório privado) | Histórico do código, fora da VPS |

---

## 3. Estrutura de pastas

```
~/stack/
├── evolution/                      # Infraestrutura de conexão com o WhatsApp
│   ├── .env                        # Senhas do Postgres, chave da Evolution API
│   ├── docker-compose.yml          # Define os containers: postgres, redis, evolution-api
│   ├── init-db.sh                  # Cria o banco "botdb" além do banco "evolution", no mesmo Postgres
│   ├── botoes.json                 # Payload de teste para botões via Evolution API (endpoint com bug — ver seção 6)
│   └── botoes_meta.json            # Payload de teste para botões via Meta Graph API (funcional)
│
├── bot-app/                        # Lógica de negócio do bot (o "produto")
│   ├── .env                        # String de conexão do banco, Redis, chaves da Evolution API e da Meta
│   ├── venv/                       # Ambiente virtual Python (não versionado no Git)
│   ├── database.py                 # Conexão SQLAlchemy com o Postgres (banco "botdb")
│   ├── models.py                   # Tabelas: Empresa, Servico, ClienteFinal, Agendamento
│   ├── redis_client.py             # Conexão com o Redis
│   ├── conversa.py                 # Máquina de estados: decide o que responder em cada etapa da conversa
│   ├── main.py                     # Recebe o webhook, identifica a empresa, chama conversa.py, envia a resposta
│   ├── criar_tabelas.py            # Script rodado uma vez, para criar as tabelas no banco
│   ├── seed.py                     # Cria a empresa de teste "Clínica Sorriso Feliz" com 3 serviços
│   ├── pausar.py / retomar.py      # Liga/desliga o bot de uma empresa específica (coluna `ativo`)
│   └── bot-app.service             # Definição do serviço systemd (cópia; original em /etc/systemd/system/)
│
└── README.md                       # Este arquivo
```

---

## 4. Modelo de dados

```sql
empresas
  id, nome, slug, segmento (clinica|barbearia|restaurante),
  telefone_whatsapp, evolution_instance_name, ativo, criado_em

servicos
  id, empresa_id (FK), nome, duracao_minutos, preco, ativo

clientes_finais
  id, empresa_id (FK), telefone, nome, criado_em

agendamentos
  id, empresa_id (FK), cliente_final_id (FK), servico_id (FK),
  data_hora, status (pendente|confirmado|cancelado|realizado)
```

Todas as tabelas de negócio têm `empresa_id`, garantindo isolamento entre clientes diferentes dentro do mesmo banco de dados.

---

## 5. Como o fluxo de uma mensagem funciona, passo a passo

1. Alguém manda uma mensagem para o WhatsApp conectado (via Baileys ou Cloud API).
2. A Evolution API recebe a mensagem e envia um `POST` para `/webhook` no `bot-app` (`main.py`).
3. `main.py` extrai o nome da instância (`payload["instance"]`) e busca, no Postgres, a empresa correspondente (`evolution_instance_name`) — e verifica se ela está `ativo = true`.
4. Se a empresa existir e estiver ativa, `main.py` chama `conversa.processar_mensagem(...)`.
5. `conversa.py` consulta o Redis para saber em que `passo` da conversa aquele número está (`novo`, `aguardando_servico`, `aguardando_horario`).
6. Dependendo do passo, `conversa.py` monta a resposta adequada (lista de serviços, pergunta de horário, confirmação de agendamento) e atualiza o `passo` no Redis.
7. No passo final, um registro é criado na tabela `agendamentos`, e o estado da conversa no Redis é apagado.
8. `main.py` envia a resposta de volta através da Evolution API (`sendText`) — ou, no caso de mensagens com botões, diretamente pela Meta Graph API.

### Filtro de ativação

Para evitar que qualquer mensagem recebida (inclusive testes acidentais, já que o número conectado à instância Baileys é pessoal) dispare o fluxo de atendimento, `conversa.py` só inicia uma nova conversa se o texto recebido bater com uma palavra-chave de ativação específica (configurável em código).

---

## 6. Decisões de arquitetura e correções feitas ao longo do desenvolvimento

Esta seção documenta os principais problemas encontrados e por que resolvemos do jeito que resolvemos — útil para não repetir o mesmo caminho de investigação no futuro.

### 6.1 — PostgreSQL em vez de SQLite
SQLite não suporta bem escrita concorrente, o que quebraria com múltiplos clientes recebendo mensagens simultaneamente. Além disso, a própria Evolution API só aceita PostgreSQL ou MySQL como banco interno — SQLite não é uma opção válida para ela. Solução: um único Postgres, compartilhado entre a Evolution API e o `bot-app`, com dois bancos de dados separados (`evolution` e `botdb`) para não gastar RAM extra rodando dois motores de banco diferentes.

### 6.2 — Portas do Postgres e Redis fechadas para a internet
Por padrão, as portas do Postgres (5432) e Redis (6379) não são expostas fora do Docker. Quando o `bot-app` (que roda fora do Docker, direto na VPS) precisou acessá-las, abrimos as portas, mas **restritas a `127.0.0.1`** (só a própria VPS pode acessar) — nunca abertas de forma genérica, para não expor esses serviços à internet.

### 6.3 — `host.docker.internal` para comunicação Evolution API → bot-app
A Evolution API roda dentro de um container Docker; o `bot-app` roda fora dele, direto na VPS. Dentro de um container, `localhost` se refere ao próprio container, não à VPS. Foi necessário adicionar `extra_hosts: host.docker.internal:host-gateway` no `docker-compose.yml` e usar `http://host.docker.internal:8000/webhook` como URL do webhook, em vez de `localhost`.

### 6.4 — Porta 8080 da Evolution API também restrita
O Docker manipula regras de rede (`iptables`) por um caminho que ignora o firewall `ufw` — publicar uma porta com `"8080:8080"` no `docker-compose.yml` a deixa acessível pela internet mesmo com o `ufw` configurado. Corrigido para `"127.0.0.1:8080:8080"`, já que nenhuma ferramenta externa precisa acessar essa porta diretamente hoje.

### 6.5 — Bug de pairing code na Evolution API
A conexão via código de pareamento (alternativa ao QR Code, mais acessível) retornou "código inválido" de forma consistente — bug conhecido dessa versão da ferramenta. Solução adotada: conexão via QR Code, extraído como imagem (`qrcode.png`) e escaneado com ajuda visual pontual de terceiros (processo necessário apenas uma vez, na configuração inicial da instância).

### 6.6 — Nome da imagem Docker da Evolution API mudou
A imagem migrou de `atendai/evolution-api` para `evoapicloud/evolution-api`. Corrigido no `docker-compose.yml`, com uma tag de versão fixa (não `latest`), para controle explícito sobre atualizações.

### 6.7 — Erro `Setting_instanceId_fkey` ao criar instância Cloud API
Causado por um token de acesso **temporário** da Meta (23h de validade) ser longo demais para a coluna do banco de dados da Evolution API. Resolvido gerando um **token permanente** através de um Usuário do Sistema na Business Manager (Meta Business Suite → Configurações → Usuários do Sistema), com formato mais compacto. Tokens permanentes, além de resolverem esse bug, são o correto para uso em produção — tokens temporários nunca devem ser usados com clientes reais.

### 6.8 — Bug confirmado no endpoint `/message/sendButtons` da Evolution API
A versão instalada (2.3.7, e também 2.3.6 após downgrade) apresenta erros ao tentar enviar mensagens com botões através da Evolution API (`"Button texts cannot be repeated"`, entre outros erros documentados publicamente pela comunidade em diferentes versões). **Decisão de arquitetura:** para mensagens interativas (botões, listas), o `bot-app` chama diretamente a Meta Graph API (`https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages`), usando o token permanente, em vez de depender do endpoint da Evolution API para esse recurso específico. A Evolution API continua responsável por receber mensagens (webhook) e enviar textos simples, onde funciona de forma estável.

### 6.9 — Janela de 24 horas (erro 131047)
A API oficial da Meta só permite o envio de mensagens de texto livre (incluindo botões) para um número dentro de 24h a partir da última mensagem recebida desse número. Fora dessa janela, é necessário usar um template pré-aprovado pela Meta. Em uso real, essa janela normalmente está aberta, pois é sempre o cliente quem inicia a conversa.

### 6.10 — Restrição de mensagens entre países (erro 130497)
A Meta restringe mensagens de negócios enviadas de um número registrado em um país para usuários em outro — especificamente entre Brasil e outros países, nas duas direções. O número de testes gratuito gerado automaticamente pela Meta tem prefixo `+1` (EUA), o que bloqueia o envio para números brasileiros. **Solução planejada:** registrar um número de telefone brasileiro real como número de produção do WhatsApp Business (processo pendente — depende da aquisição de um chip dedicado, para não comprometer o número pessoal do desenvolvedor nem o número já usado na instância Baileys).

---

## 7. Instâncias da Evolution API atualmente configuradas

| Nome da instância | Tipo de conexão | Finalidade |
|---|---|---|
| `teste-aprendizado` | Baileys (WhatsApp pessoal, via QR Code) | Testes gerais, validação da máquina de estados, fluxo de agendamento completo |
| `clinica-oficial` | Cloud API oficial (Meta) | Testes da API oficial — token permanente configurado; envio de botões bloqueado até registro de número brasileiro de produção |

---

## 8. Segurança — pontos já endereçados

- `.env` (em ambas as pastas) está no `.gitignore` — nunca commitado no Git.
- Portas do Postgres, Redis e Evolution API restritas a `127.0.0.1` (não acessíveis pela internet).
- Firewall (`ufw`) habilitado, liberando apenas SSH e a porta 8000 (`bot-app`).
- Swap de 1 GB configurado na VPS, para evitar quedas por falta de memória.
- Tokens da Meta tratados como segredo — nunca commitados ou compartilhados publicamente; token temporário substituído por token permanente de Usuário do Sistema.

## 8.1 — Pendências de segurança / produção
- [ ] Configurar HTTPS (Nginx ou Caddy) na frente da porta 8000/8080, antes de conectar qualquer cliente real.
- [ ] Avaliar rotação/expiração do token permanente da Meta.
- [ ] Adicionar autenticação no painel administrativo (ainda não construído).

---

## 9. Comandos úteis do dia a dia

```bash
# Ver status dos containers de infraestrutura
cd ~/stack/evolution && docker compose ps

# Ver logs da Evolution API
docker compose logs evolution-api --tail=80

# Reiniciar o bot-app depois de alterar código
sudo systemctl restart bot-app

# Ver status do bot-app
sudo systemctl status bot-app

# Pausar/retomar o bot de uma empresa específica
cd ~/stack/bot-app && source venv/bin/activate
python pausar.py
python retomar.py

# Consultar agendamentos direto no banco
docker exec -it stack-postgres psql -U stackuser -d botdb -c "SELECT * FROM agendamentos;"
```

---

## 10. Roadmap — o que falta para virar produto vendável

- [ ] Reconhecer respostas de botão no `main.py` (formato de payload diferente do texto digitado).
- [ ] Registrar número de produção brasileiro na Meta e recriar a instância Cloud API correspondente.
- [ ] Painel administrativo web (login, edição de horários/serviços, visualização de agendamentos) — hoje só acessível via terminal/SQL.
- [ ] Interpretação de datas/horários em linguagem natural (hoje o texto é salvo literalmente, sem validação).
- [ ] Migrar o `bot-app` para dentro do Docker também, unificando o ambiente.
- [ ] Cobrança recorrente (Mercado Pago Assinaturas ou Stripe).
- [ ] Processo formal de onboarding de novo cliente (script de cadastro).
- [ ] Vídeo de demonstração e início da prospecção comercial.

---

## 11. Histórico de aprendizado

Este projeto foi construído por um desenvolvedor iniciante em Docker, Evolution API e desenvolvimento de bots de WhatsApp — usando exclusivamente teclado e leitor de tela (NVDA), via terminal e VS Code com Remote-SSH. Cada decisão técnica documentada acima foi validada na prática, não apenas copiada de tutorial, incluindo a identificação e correção de bugs reais da Evolution API não documentados de forma centralizada pela própria ferramenta.
