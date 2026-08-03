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
6. A resposta volta como mensagem interativa (botão ou lista) pela Meta Graph API. A Evolution API hoje só recebe o webhook e sobe a infraestrutura de WhatsApp — o envio de mensagens é sempre pela Meta.
7. Um ciclo em segundo plano varre agendamentos próximos e envia lembrete automático via WhatsApp (template pré-aprovado da Meta).
8. Quando a máquina de estados não consegue determinar o próximo passo, o bot primeiro consulta a base de conhecimento cadastrada da empresa (match determinístico, sem IA) e só depois, se não achar nada, cai na camada opcional de IA — antes de cair na resposta genérica de "não entendi".

## Stack técnica

| Camada | Tecnologia | Papel |
|---|---|---|
| API do bot | Python + FastAPI + Uvicorn | Recebe webhooks e orquestra o fluxo |
| Persistência | PostgreSQL + SQLAlchemy | Guarda empresas, serviços, clientes e agendamentos |
| Estado de conversa | Redis | Mantém o passo atual da conversa por 30 minutos |
| Integração WhatsApp | Evolution API | Recebe os webhooks de mensagem e sobe a infraestrutura de WhatsApp |
| Mensagens interativas | Meta Graph API | Envia botões, listas e templates (todo o envio de mensagem passa por aqui) |
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
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── cache.py
│   │   ├── models.py
│   │   ├── prompts.py
│   │   ├── provider.py
│   │   └── service.py
│   ├── conhecimento.py
│   ├── configuracoes.py
│   ├── conversa.py
│   ├── criar_tabelas.py
│   ├── database.py
│   ├── lembretes.py
│   ├── main.py
│   ├── meta_client.py
│   ├── metricas.py
│   ├── models.py
│   ├── pausar.py
│   ├── redis_client.py
│   ├── retomar.py
│   ├── texto_utils.py
│   ├── templates/
│   │   └── admin/
│   │       ├── agendamentos_list.html
│   │       ├── base.html
│   │       ├── cliente_detail.html
│   │       ├── clientes_inativos.html
│   │       ├── clientes_list.html
│   │       ├── configuracoes.html
│   │       ├── conhecimento_form.html
│   │       ├── conhecimento_list.html
│   │       ├── dashboard.html
│   │       ├── empresa_form.html
│   │       ├── empresas_list.html
│   │       ├── insights.html
│   │       ├── login.html
│   │       ├── servico_form.html
│   │       ├── servicos_list.html
│   │       └── solicitacoes_atendimento_list.html
│       └── onboarding/
│           ├── base.html
│           ├── setup.html
│           └── success.html
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
- [bot-app/meta_client.py](/home/wadson/stack/bot-app/meta_client.py) envia botões, listas e templates pela API oficial da Meta.
- [bot-app/lembretes.py](/home/wadson/stack/bot-app/lembretes.py) varre agendamentos próximos e dispara o lembrete automático via template.
- [bot-app/ai/](/home/wadson/stack/bot-app/ai/) contém a camada de interpretação de linguagem natural (NLU) usada como fallback pela máquina de estados — ver seção "Camada de IA (NLU)".
- [bot-app/conhecimento.py](/home/wadson/stack/bot-app/conhecimento.py) é o CRUD e a busca da base de conhecimento por empresa — ver seção "Base de conhecimento".
- [bot-app/configuracoes.py](/home/wadson/stack/bot-app/configuracoes.py) é a fonte única de configuração operacional (Meta, IA, lembretes, ativação do bot) — ver seção "Configurações pelo painel".
- [bot-app/metricas.py](/home/wadson/stack/bot-app/metricas.py) concentra as consultas agregadas do dashboard, dos insights e da lista de clientes inativos — mantém `admin.py` como camada de rota, sem regra de negócio embutida.
- [bot-app/texto_utils.py](/home/wadson/stack/bot-app/texto_utils.py) normaliza texto (acentos, caixa, espaços) — usado por `conversa.py` e `conhecimento.py`.
- [bot-app/database.py](/home/wadson/stack/bot-app/database.py) e [bot-app/redis_client.py](/home/wadson/stack/bot-app/redis_client.py) centralizam conexões.
- [evolution/docker-compose.yml](/home/wadson/stack/evolution/docker-compose.yml) sobe a infraestrutura de apoio.
- [Caddyfile](/home/wadson/stack/Caddyfile) faz o reverse proxy para a porta local do bot.

## Modelo de dados

O banco trabalha com estas tabelas principais:

- `empresas`: identifica cada cliente atendido pelo bot.
- `servicos`: serviços disponíveis por empresa.
- `clientes_finais`: pessoas que conversaram com o bot.
- `agendamentos`: registros criados ao final do fluxo.
- `solicitacoes_atendimento`: pedidos de atendimento humano.
- `empresa_conhecimento`: perguntas e respostas cadastradas por empresa (base de conhecimento).
- `conversas_iniciadas`: log mínimo (empresa, telefone, data) de quando uma conversa nova começa — existe só para alimentar as métricas de "conversas iniciadas" e "taxa de conversão" do dashboard, que não são calculáveis a partir de nenhuma outra tabela (o estado da conversa em si vive só no Redis, com TTL de 30 minutos).
- `configuracao_sistema`: linha única (`id=1`) com as configurações operacionais editáveis em `/admin/configuracoes` — ver seção "Configurações pelo painel".

O isolamento é multi-tenant por coluna `empresa_id`, então uma mesma base atende várias empresas sem misturar os dados.

## Fluxo de conversa

O fluxo hoje é uma máquina de estados simples baseada em palavra-chave. A IA (ver "Camada de IA (NLU)") não substitui essa máquina de estados — só entra como intérprete de último recurso quando nenhuma regra da máquina de estados sabe o que fazer com a mensagem.

- Estado inicial: ignora mensagens aleatórias e só abre conversa quando a mensagem bate com a palavra de ativação `oibot` ou quando o usuário toca em uma ação prevista.
- Lista de serviços: busca os serviços ativos da empresa e envia uma lista interativa.
- Escolha do período: apresenta botões para manhã, tarde ou texto livre.
- Confirmação: cria cliente e agendamento, salva no banco e limpa o estado do Redis.
- Menu guiado: quando a mensagem não encaixa no fluxo atual, o bot mostra um menu com atalhos para serviços, reagendamento, cancelamento e atendimento humano.
- Cancelamento com confirmação: o bot pede confirmação antes de cancelar um agendamento ativo.
- Reagendamento: o bot tenta recuperar o agendamento ativo do número quando o estado já expirou.
- Fallbacks: respostas inesperadas não silenciam mais a conversa; o bot orienta o próximo passo de forma explícita.
- Atendimento humano: quando o usuário pede uma pessoa, o bot registra a solicitação em `solicitacoes_atendimento` e confirma o envio.

Também existem atalhos de controle como `menu`, `voltar` e `cancelar`, que reiniciam a conversa.

## Lembretes automáticos

Um ciclo assíncrono roda dentro do próprio processo do `bot-app` (sem serviço externo, sem dependência nova), varrendo a cada `LEMBRETE_INTERVALO_MINUTOS` os agendamentos com status `agendado` ou `confirmado` cujo horário cai dentro da janela de `LEMBRETE_ANTECEDENCIA_HORAS`. Cada agendamento recebe no máximo um lembrete (controlado pela coluna `agendamentos.lembrete_enviado_em`); reagendar um horário libera um novo lembrete para a nova data.

**Pré-requisito obrigatório (fora do código):** a Graph API do WhatsApp só permite mensagem livre dentro da janela de 24h de atendimento ao cliente. Como o lembrete é enviado proativamente, ele precisa de um **template de mensagem aprovado no Meta Business Manager**. Sem esse cadastro, os lembretes vão falhar silenciosamente (o erro fica registrado em log, sem quebrar a aplicação) até o template ser aprovado.

Cadastre no Meta Business Manager:

- Nome: `lembrete_agendamento` (ou o valor configurado em `META_TEMPLATE_LEMBRETE_NOME`)
- Categoria: `UTILITY`
- Idioma: Portuguese (BR) — `pt_BR` (ou o valor configurado em `META_TEMPLATE_LEMBRETE_IDIOMA`)
- Corpo: `Olá {{1}}! Passando para lembrar do seu horário de {{2}} marcado para {{3}} na {{4}}. Para cancelar, é só responder esta mensagem.`
- Exemplos para submissão: `{{1}}=Maria`, `{{2}}=Corte de cabelo`, `{{3}}=15/08/2026 às 14:00`, `{{4}}=Clínica Sorriso Feliz`

Nome/idioma do template, antecedência e intervalo de verificação são editáveis em `/admin/configuracoes` (ver "Configurações pelo painel") — o `.env.example` só documenta o valor inicial.

## Base de conhecimento

Cada empresa tem sua própria base de perguntas e respostas (`empresa_conhecimento`), cadastrada em `/admin/conhecimento` — CRUD completo (criar, editar, ativar/desativar, excluir logicamente), no mesmo padrão visual do CRUD de Serviços. Exemplos típicos: "Aceita Unimed?" → "Sim, atendemos Unimed.", "Tem estacionamento?" → "Sim, gratuito."

Essa base é consultada **antes** da IA (ver diagrama abaixo) e usa matching determinístico em Python — [bot-app/conhecimento.py](/home/wadson/stack/bot-app/conhecimento.py), função `buscar_resposta()` — não é busca semântica/embeddings, é uma heurística simples de sobreposição de palavras (com prefixo de 5 caracteres para tolerar variações como "aceita"/"aceitam") entre a mensagem do cliente e cada pergunta ativa da empresa. Isso é proposital: garante que **uma resposta cadastrada nunca seja substituída por algo inventado**, porque quando o limiar de confiança é atingido, a resposta cadastrada é enviada direto, sem nenhuma chamada de IA.

## Camada de IA (NLU)

A IA **não substitui** a máquina de estados — ela é só uma camada de interpretação de linguagem natural, acionada apenas quando a máquina de estados não sabe o que fazer com a mensagem, e só depois de a base de conhecimento já ter sido consultada:

```
WhatsApp → Webhook → Máquina de estados (conversa.py)
                            │
                            ├─ interpretou a mensagem → segue o fluxo normal (sem IA)
                            │
                            └─ não conseguiu interpretar
                                     │
                                     ▼
                    conhecimento.buscar_resposta() (empresa_conhecimento)
                                     │
                        ├─ achou uma pergunta cadastrada → responde direto, sem IA
                                     │
                                     └─ não achou
                                              │
                                              ▼
                                      AIService.interpretar()
                                              │
                                  (cache → provider → timeout → fallback)
                                              │
                                              ▼
                                  intent + entidades tipados
                                              │
                                              ▼
                            máquina de estados decide a ação (ou mantém
                            o fallback padrão de sempre, se nada ajudar)
```

Hoje o único ponto de integração é o fallback final de `processar_mensagem` — na prática, isso cobre principalmente mensagens livres de um cliente que já tem agendamento confirmado (`passo == "agendamento_ativo"`), quando ele escreve algo que não bate com nenhuma palavra-chave (`_texto_corresponde`) mas expressa uma intenção real, como "não vou poder ir nesse horário", "posso trocar pra sexta?" ou "tem estacionamento?". Nenhum handler de estado existente foi alterado.

Intents reconhecidos e para onde cada um é roteado (sempre reaproveitando um handler que já existe — a IA nunca age direto no banco):

| Intent | Ação |
|---|---|
| `cancelar` | Pede confirmação de cancelamento (mesmo fluxo de sempre — a IA nunca cancela direto) |
| `reagendar` | Mostra horários disponíveis para reagendamento |
| `consultar_servicos` / `consultar_precos` | Mostra a lista de serviços (já traz o preço de cada um) |
| `falar_com_atendente` | Registra solicitação de atendimento humano |
| `saudacao` | Mostra o menu principal |
| `agendar`, `consultar_horarios`, `desconhecido` | Mantém a resposta de fallback padrão de sempre (nenhuma ação nova nesta primeira versão) |

Módulos em [bot-app/ai/](/home/wadson/stack/bot-app/ai/):

- `models.py` — tipos: `Intent` (enum), `Entidades`, `InterpretacaoIA`.
- `provider.py` — interface `AIProvider` (um único método, `completar(mensagens) -> str`, no formato `role`/`content` já padrão OpenAI) + implementação `OpenAIProvider`.
- `prompts.py` — o prompt de sistema que instrui o modelo a responder só em JSON.
- `cache.py` — cache de interpretação no Redis (reaproveita `redis_client.redis_cliente`, mesma conexão do estado da conversa), isolado por `empresa_id`.
- `service.py` — `AIService`, a única porta de entrada usada pelo resto do sistema: aplica cache, timeout (`asyncio.wait_for`, além do timeout do próprio client) e nunca deixa uma exceção do provedor vazar — qualquer falha vira um resultado `desconhecido` e a máquina de estados segue com o fallback padrão.

**Desligada por padrão.** Sem configurar nada, o comportamento do bot é idêntico ao de antes desta camada existir. Habilitada, provedor, chave de API, modelo, timeout e TTL do cache são todos editáveis em `/admin/configuracoes` (ver "Configurações pelo painel") — não exige reiniciar o processo.

### Como adicionar um novo provider (Claude, Gemini, Ollama...)

1. Implemente uma classe em `ai/provider.py` que herda de `AIProvider` e implementa só `async def completar(self, mensagens: list[dict]) -> str`, recebendo mensagens no formato `[{"role": "system"|"user", "content": "..."}]` e devolvendo o texto bruto da resposta. Capture as exceções específicas do SDK do provider e relance como `AIProviderError` — o resto do sistema não deve conhecer exceções específicas de nenhum provider.
2. Registre o novo valor de `AI_PROVIDER` em `criar_ai_service()` (`ai/service.py`), instanciando a nova classe.
3. Nenhum outro arquivo precisa mudar — `AIService`, o cache, o prompt e a integração em `conversa.py` são todos agnósticos de provider.

## Configurações pelo painel

`/admin/configuracoes` reúne, num formulário único (é configuração global do sistema, não por empresa), tudo que antes só dava pra trocar editando o `.env` e reiniciando o processo:

- Meta Graph API: token, Phone Number ID, Business ID.
- Palavra(s) de ativação do bot.
- Lembretes automáticos: nome/idioma do template, antecedência em horas, intervalo de verificação em minutos.
- Camada de IA: habilitada, provedor, chave de API, modelo, timeout, TTL do cache.

**Fica de fora, de propósito** — não é esquecimento:
- `DATABASE_URL`/`REDIS_URL`/`EVOLUTION_*`: bootstrap — o processo precisa delas antes mesmo de conseguir ler qualquer coisa do banco.
- `ADMIN_USERNAME`/`ADMIN_PASSWORD`/`SESSION_SECRET_KEY`: identidade/sessão do próprio painel, assunto diferente (trocar senha merece hashing e invalidação de sessão próprios).
- `SEED_EMPRESA_*`: só usados pelo script `seed.py`, sem relevância em runtime.

**Como funciona sem restart:** tudo isso fica numa tabela `configuracao_sistema` (linha única). Na primeira vez que o sistema lê essa tabela, ela é criada copiando os valores atuais do `.env` — depois disso, o banco é a fonte viva e o `.env` vira só o valor inicial. Os módulos que antes liam `settings.*` uma única vez no import (`meta_client.py`, `ai/service.py`, `lembretes.py`, o loop de lembretes em `main.py`) passaram a consultar [bot-app/configuracoes.py](/home/wadson/stack/bot-app/configuracoes.py) a cada chamada/ciclo — uma mudança salva no painel vale na próxima mensagem ou no próximo ciclo do lembrete, sem precisar reiniciar o bot. Construir o client da IA por mensagem é barato (não faz chamada de rede no `__init__`), e uma consulta por chave primária no banco tem custo desprezível no volume de mensagens de um bot de pequena empresa — por isso não há cache/TTL nessa leitura.

**Segredos nunca voltam pro navegador**: os campos de token/chave de API usam `input type="password"` sempre vazio ao carregar a página (só um texto indicando se já há valor salvo); deixar em branco no submit mantém o valor atual, preencher substitui.

## Dashboard, insights e clientes inativos

Todas as consultas agregadas ficam em [bot-app/metricas.py](/home/wadson/stack/bot-app/metricas.py) — `admin.py` só chama e renderiza, sem regra de negócio na rota.

**Dashboard** (`/admin/dashboard`) ganhou um filtro de período (`data_inicio`/`data_fim`, padrão últimos 30 dias) e uma seção nova com 10 indicadores: conversas iniciadas, agendamentos realizados, cancelamentos, solicitações de atendimento, taxa de conversão, serviço mais solicitado, horário mais solicitado, dia da semana mais movimentado, clientes novos e clientes recorrentes. Os cards antigos (totais gerais, sem filtro de período) continuam exatamente como estavam antes desta sprint.

Duas decisões de definição, documentadas aqui porque não são óbvias só olhando o código:
- **Cliente recorrente** = dentre os que agendaram no período filtrado, quantos já tinham mais de 1 agendamento no total (histórico completo, não só do período).
- **Serviço/horário/dia mais frequentes** são calculados em Python (`collections.Counter`) a partir de uma projeção estreita (`data_hora` + nome do serviço, não a linha inteira) em vez de SQL agregado, de propósito: extração de hora/dia-da-semana usa funções diferentes em SQLite (testes) e Postgres (produção), e agregar em Python evita acoplar o código a um dialeto — o volume de agendamentos por período de uma empresa pequena não justifica essa otimização.

**Insights** (`/admin/insights`) reaproveita a mesma `calcular_metricas()` do dashboard e monta frases só com números já calculados (nunca texto ou dado inventado) — ex.: "O serviço Limpeza representa 42% dos agendamentos dos últimos 30 dias.", "18 clientes não retornam há mais de 90 dias.", "O índice de cancelamento aumentou nesta semana em relação à semana anterior." Se não houver dado suficiente para um insight específico, a frase correspondente simplesmente não aparece.

**Clientes inativos** (`/admin/clientes-inativos`) reaproveita a mesma estrutura de query de `/admin/clientes`, com seletor de 30/60/90/180 dias. Um cliente é considerado inativo quando sua última interação (o agendamento mais recente, ou a data de cadastro se nunca agendou) é mais antiga que o corte selecionado. A tela existe separada da listagem geral de clientes de propósito — no futuro vai servir de base para campanhas de retorno.

## Estado atual

O que já existe e funciona hoje:

- Configuração centralizada em [bot-app/config.py](/home/wadson/stack/bot-app/config.py).
- Arquivo de exemplo de ambiente em [bot-app/.env.example](/home/wadson/stack/bot-app/.env.example).
- Dependências fixadas em [bot-app/requirements.txt](/home/wadson/stack/bot-app/requirements.txt).
- Bot containerizado com [bot-app/Dockerfile](/home/wadson/stack/bot-app/Dockerfile) e [bot-app/docker-compose.yml](/home/wadson/stack/bot-app/docker-compose.yml).
- Rotas de saúde `/healthz` e `/readyz` ativas no FastAPI.
- Painel administrativo com login, dashboard, cadastro de empresas, cadastro de serviços, listagem de agendamentos e solicitações de atendimento.
- Painel de clientes (`/admin/clientes`) com busca, ordenação e página de detalhe mostrando histórico de agendamentos e de solicitações de atendimento de cada cliente final.
- Base de conhecimento por empresa (`/admin/conhecimento`), com CRUD completo e busca usada pelo bot antes de recorrer à IA.
- Dashboard com métricas de empresas, clientes, serviços, agendamentos do dia e solicitações pendentes, com filtro por empresa, mais 10 indicadores por período (conversas, conversão, cancelamento, recorrência, etc.).
- Página de Insights com frases geradas a partir dos dados reais da operação.
- Listagem de clientes inativos com corte configurável (30/60/90/180 dias).
- Onboarding público em `/onboarding`, permitindo cadastrar empresa, configurar WhatsApp e criar o primeiro serviço sem usar terminal.
- Recebimento de webhooks da Evolution API.
- Identificação da empresa pela instância recebida no payload.
- Máquina de estados com Redis e expiração de 30 minutos.
- Cadastro de empresa, serviços e cliente final.
- Registro de agendamento no banco com validação de janela, conflito e horário de funcionamento.
- Reagendamento e cancelamento operacionais no fluxo do bot.
- Registro de solicitações de atendimento humano com prevenção de duplicidade por telefone e empresa.
- Menu de atendimento, confirmação de cancelamento, encaminhamento para humano e respostas de fallback mais naturais.
- Envio de mensagens interativas pela Meta Graph API.
- Script de seed com uma empresa de teste: `Clínica Sorriso Feliz`.
- Scripts simples para pausar e retomar a empresa de teste.
- Bootstrap automático de schema para ambientes já existentes.
- Wizard público de onboarding para novos clientes, com validação e tela de sucesso.
- Lembrete automático de agendamento via WhatsApp (template Meta), com controle de envio único por agendamento e reset automático em reagendamento.
- Camada de IA (NLU) opcional como fallback da máquina de estados, com provider OpenAI, cache Redis por empresa, timeout e fallback seguro — desligada por padrão.
- Configurações operacionais (Meta, ativação do bot, lembretes, IA) editáveis em `/admin/configuracoes`, valendo sem precisar reiniciar o processo.

O que ainda está faltando para virar produto de fato:

- Multi-tenant mais robusto, com papéis de usuário e permissões no painel.
- Cadastro manual de cliente final direto pelo painel (hoje o cliente só é criado a partir de uma conversa real no WhatsApp).
- Fluxo de cobrança.
- Observabilidade e auditoria mais completas.
- Ampliar a cobertura de testes automatizados (hoje cobre onboarding, conversa, solicitações de atendimento, o painel de clientes, os lembretes automáticos, a camada de IA, a base de conhecimento, as métricas/insights e as configurações do painel).
- IA cobrindo mais pontos de fallback (hoje só o catch-all final) e mais providers além da OpenAI.
- Base de conhecimento usando matching por palavra-chave/prefixo (v1); pode evoluir para busca semântica se o volume de perguntas justificar.
- Clientes inativos ainda não vira campanha de verdade — hoje é só listagem.
- Configurações sensíveis (token da Meta, chave de IA) ficam em texto puro no banco, sem criptografia em repouso — consistente com o resto do projeto hoje (ex.: senha do admin também não é hasheada), mas é um ponto a evoluir junto.

## Limitações e bugs conhecidos

Esses pontos aparecem no código atual e devem ser tratados como limitações reais, não como detalhe de documentação.

- O fluxo principal ainda depende de regras explícitas e botões/listas; a camada de IA (NLU) existe só como fallback pontual, desligada por padrão, e cobre apenas o catch-all final da máquina de estados — não é um bot conversacional livre.
- O reconhecimento de botões/listas ainda depende bastante do texto exibido; o `id` da interação existe no webhook, mas não é usado em todos os caminhos.
- Quando a empresa não tem serviços ativos, o bot ainda orienta o usuário com uma resposta simples, mas a operação continua dependente da configuração do catálogo.
- A primeira versão do atendimento humano registra e organiza a demanda, mas ainda não distribui automaticamente a solicitação para um operador específico.

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

- Criptografia em repouso para segredos guardados no banco (token da Meta, chave de IA) — hoje ficam em texto puro, como o resto das credenciais do projeto.
- Validar e renovar tokens da Meta com processo controlado — hoje dá pra trocar o token pelo painel sem reiniciar o processo, mas não há verificação automática de validade/expiração.
- Rever logs, alertas e política de backup.

O painel administrativo já exige login (usuário/senha via variável de ambiente). Trocar o token da Meta ou a chave de IA não exige mais editar `.env` e reiniciar o processo — é feito em `/admin/configuracoes` (ver seção "Configurações pelo painel").

## Melhorias futuras

Se o objetivo for transformar isso em um produto vendável, os próximos passos mais úteis são:

1. Fortalecer permissões e papéis no painel administrativo.
2. Ampliar a cobertura de testes automatizados.
3. Acrescentar cobrança recorrente.
4. Melhorar observabilidade com métricas e alertas operacionais.

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
