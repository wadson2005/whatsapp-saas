# Arquitetura

Este documento descreve como as peças do sistema se encaixam e por que algumas decisões de design foram tomadas. Para instalação e execução, veja o [README](../README.md); para deploy em produção, veja [deployment.md](deployment.md).

## Caminho de uma mensagem

1. O cliente manda uma mensagem no WhatsApp.
2. A Evolution API recebe e dispara um webhook para `POST /webhook` (`bot-app/main.py`).
3. O bot identifica a instância recebida no payload, procura a empresa correspondente (`evolution_instance_name`) e confirma que ela está ativa.
4. `conversa.processar_mensagem()` consulta o estado atual da conversa no Redis (chave `conversa:{empresa_id}:{numero}`, TTL configurável por empresa) e decide o próximo passo.
5. Se a máquina de estados não sabe o que fazer com a mensagem, ela consulta primeiro a base de conhecimento da empresa e, só depois, a camada de IA opcional (ver abaixo).
6. O agendamento, quando confirmado, é validado e gravado em PostgreSQL (`services/agenda.py`) — conflito de horário, dia de funcionamento, horário de almoço e dias/datas indisponíveis são todos verificados antes de persistir.
7. A resposta volta como mensagem interativa (botão ou lista) via Meta Graph API (`integrations/meta_client.py`). A Evolution API só recebe o webhook e sobe a infraestrutura de WhatsApp — todo envio de mensagem passa pela Meta.
8. Em paralelo, um ciclo assíncrono dentro do próprio processo (`main._loop_lembretes`) varre agendamentos próximos e dispara lembretes automáticos via template pré-aprovado da Meta.

## Máquina de estados da conversa

O fluxo é uma máquina de estados baseada em palavra-chave e cliques de botão/lista, implementada em `conversa.py`. A IA não substitui essa máquina — ela só entra como intérprete de último recurso quando nenhuma regra sabe o que fazer com a mensagem.

- **Estado inicial (`novo`)** — ignora mensagens aleatórias e só abre conversa quando a mensagem bate com a palavra de ativação configurada (`oibot` por padrão) ou quando o usuário toca em uma ação prevista.
- **Lista de serviços** — busca os serviços ativos da empresa e envia uma lista interativa.
- **Escolha de período** — botões para manhã, tarde ou "prefiro digitar" (texto livre).
- **Confirmação** — cria o cliente final (se ainda não existir) e o agendamento, grava no banco e limpa o estado do Redis.
- **Menu guiado** — quando a mensagem não encaixa no fluxo atual, mostra atalhos para serviços, reagendamento, cancelamento e atendimento humano.
- **Cancelamento com confirmação** — sempre pede confirmação explícita antes de cancelar (a IA, quando aciona esse fluxo, também passa por aqui — nunca cancela direto).
- **Reagendamento** — recupera o agendamento ativo do número mesmo quando o estado da conversa já expirou no Redis.
- **Fallback contextual** — a mensagem de erro muda de acordo com o passo atual da conversa; o cliente nunca fica sem resposta.

Atalhos globais como `menu`, `voltar` e `cancelar` funcionam em qualquer ponto da conversa, não só no passo esperado.

## Base de conhecimento

Cada empresa tem sua própria base de perguntas e respostas (tabela `empresa_conhecimento`), gerenciada em `/admin/conhecimento` com CRUD completo (criar, editar, ativar/desativar, excluir logicamente).

A busca (`services/conhecimento.buscar_resposta()`) é consultada **antes** da IA e usa matching determinístico em Python — não é busca semântica/embeddings, é uma heurística de sobreposição de palavras (com prefixo de 5 caracteres para tolerar variações como "aceita"/"aceitam") entre a mensagem do cliente e cada pergunta ativa da empresa. Isso é proposital: garante que **uma resposta cadastrada nunca seja substituída por algo inventado** — quando o limiar de confiança é atingido, a resposta cadastrada é enviada direto, sem nenhuma chamada de IA.

## Camada de IA (NLU)

A IA **não substitui** a máquina de estados. Ela é acionada apenas quando a máquina de estados não sabe o que fazer com a mensagem, e só depois de a base de conhecimento já ter sido consultada:

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
                            o fallback padrão, se nada ajudar)
```

O único ponto de integração hoje é o fallback final de `processar_mensagem` — na prática, cobre principalmente mensagens livres de um cliente que já tem agendamento confirmado, quando ele escreve algo que não bate com nenhuma palavra-chave mas expressa uma intenção real, como "não vou poder ir nesse horário" ou "tem estacionamento?".

Intents reconhecidos e para onde cada um é roteado (sempre reaproveitando um handler que já existe — a IA nunca age direto no banco):

| Intent | Ação |
|---|---|
| `cancelar` | Pede confirmação de cancelamento (mesmo fluxo de sempre) |
| `reagendar` | Mostra horários disponíveis para reagendamento |
| `consultar_servicos` / `consultar_precos` | Mostra a lista de serviços |
| `falar_com_atendente` | Registra solicitação de atendimento humano |
| `saudacao` | Mostra o menu principal |
| `agendar`, `consultar_horarios`, `desconhecido` | Mantém a resposta de fallback padrão |

Módulos em `bot-app/ai/`:

- `models.py` — tipos: `Intent` (enum), `Entidades`, `InterpretacaoIA`.
- `provider.py` — interface `AIProvider` (um único método, `completar(mensagens) -> str`) + implementação `OpenAIProvider`.
- `prompts.py` — o prompt de sistema que instrui o modelo a responder só em JSON.
- `cache.py` — cache de interpretação no Redis, isolado por `empresa_id`.
- `service.py` — `AIService`, a única porta de entrada usada pelo resto do sistema: aplica cache, timeout (`asyncio.wait_for`) e nunca deixa uma exceção do provedor vazar — qualquer falha vira um resultado `desconhecido`.

Desligada por padrão — sem configurar nada, o comportamento do bot é idêntico ao de não ter essa camada. Habilitada, provider, chave de API, modelo, timeout e TTL do cache são editáveis em `/admin/configuracoes`, sem reiniciar o processo.

### Adicionando um novo provider de IA

1. Implemente uma classe em `ai/provider.py` que herda de `AIProvider` e implementa só `async def completar(self, mensagens: list[dict]) -> str`, recebendo mensagens no formato `[{"role": "system"|"user", "content": "..."}]`. Capture as exceções específicas do SDK do provider e relance como `AIProviderError`.
2. Registre o novo valor de `AI_PROVIDER` em `criar_ai_service()` (`ai/service.py`), instanciando a nova classe.
3. Nenhum outro arquivo precisa mudar — `AIService`, o cache, o prompt e a integração em `conversa.py` são agnósticos de provider.

## Conexão do WhatsApp (Evolution API)

Criar uma empresa cria e conecta a instância na Evolution API automaticamente — não há mais passo manual (criar instância, escanear QR, configurar webhook) fora do sistema.

`integrations/evolution_client.py` centraliza as chamadas HTTP (mesmo padrão de `meta_client.py`: um helper único, erro de rede nunca propaga cru, vira `EvolutionAPIError`):

- `criar_instancia(nome, numero, webhook_url)` — `POST /instance/create`, já com o webhook (`MESSAGES_UPSERT`, `CONNECTION_UPDATE`) configurado no mesmo payload.
- `gerar_qrcode(nome, numero)` — `GET /instance/connect/{instance}`, retorna `pairingCode` (código curto para digitar em "Conectar com número de telefone") e `code` (dado bruto do QR). A resposta de `criar_instancia` **não** traz o QR — por isso essa segunda chamada é sempre necessária logo depois de criar a instância.
- `estado_conexao(nome)` — `GET /instance/connectionState/{instance}`, retorna `open`/`connecting`/`close`.
- `excluir_instancia(nome)` — `DELETE /instance/delete/{instance}`, usado só para desfazer uma criação parcial (nunca levanta exceção — é sempre uma limpeza best-effort).

**Onboarding** (`main.py`): o nome da instância deixou de ser digitado — é sempre igual ao `slug` da empresa (já validado único). Em `POST /onboarding/configurar`, a ordem importa: primeiro chama `criar_instancia`; só grava a `Empresa` no banco se isso funcionar. Se a criação no banco falhar depois (ex.: `IntegrityError`), `excluir_instancia` desfaz a instância já criada — evita empresa cadastrada sem bot funcionando ou instância órfã na Evolution. Depois de criar, o fluxo vai para `/onboarding/conectar`, que mostra o QR/código de pareamento e faz polling em `/onboarding/conectar/status` a cada ~3s até `state == "open"`.

**Painel** (`admin.py`): `POST /admin/empresas/nova` segue a mesma lógica do onboarding — nome da instância = `slug`, `criar_instancia` roda antes de gravar a `Empresa`, e um `IntegrityError` no commit desfaz a instância via `excluir_instancia`. O formulário não pede mais o nome da instância (campo removido); depois de criar, redireciona direto para `/admin/empresas/{id}/conectar` para escanear o QR. Essa rota também é reusada para reconectar uma empresa já existente cuja sessão do WhatsApp caiu — celular trocado, dispositivo desvinculado etc. Restrito a `require_papel_admin`/`require_superadmin` (`load_empresa` garante que não dá para reconectar a instância de outra empresa).

O HTML/JS de exibir o QR e fazer o polling é um único partial (`templates/partials/qrcode_connect.html`) incluído tanto pelo onboarding quanto pelo painel — o QR é desenhado no navegador (lib `qrcode-generator` via CDN) a partir do campo `code`; o `pairingCode` é sempre mostrado como texto, como alternativa que não depende de nenhuma lib de imagem.

Requer a variável `PUBLIC_BASE_URL` (URL pública do bot) para montar a URL de webhook passada à Evolution API na criação da instância.

## Configurações pelo painel

`/admin/configuracoes` reúne, num formulário único (configuração global do sistema, não por empresa), tudo que antes só dava para trocar editando o `.env` e reiniciando o processo: credenciais da Meta, palavra de ativação do bot, parâmetros de lembrete automático e configuração completa da camada de IA.

Ficam de fora, de propósito:

- `DATABASE_URL` / `REDIS_URL` / `EVOLUTION_*` — bootstrap: o processo precisa delas antes mesmo de conseguir ler qualquer coisa do banco.
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `SESSION_SECRET_KEY` — identidade/sessão do próprio painel.
- `SEED_EMPRESA_*` — só usados pelo script `scripts/seed.py`.

**Como funciona sem restart:** os valores ficam numa tabela `configuracao_sistema` (linha única). Na primeira leitura, essa tabela é criada copiando os valores do `.env` — depois disso, o banco é a fonte viva e o `.env` vira só o valor inicial. Módulos que antes liam `settings.*` uma única vez no import (`meta_client.py`, `ai/service.py`, `lembretes.py`, o loop de lembretes) passaram a consultar `services/configuracoes.py` a cada chamada/ciclo, então uma mudança salva no painel vale na próxima mensagem ou no próximo ciclo do lembrete.

Campos sensíveis (token da Meta, chave de IA) usam `input type="password"` sempre vazio ao carregar a página; deixar em branco no submit mantém o valor atual, preencher substitui.

## Autenticação e papéis no painel

O painel tem dois tipos de login, resolvidos nessa ordem em `admin.login_submit`:

1. **Superadmin da plataforma** — as credenciais de `ADMIN_USERNAME`/`ADMIN_PASSWORD` (`.env`), comparadas com `hmac.compare_digest`. Enxerga e edita todas as empresas, é o único que acessa `/admin/empresas` (criar/listar/desativar empresas) e `/admin/configuracoes` (configuração global do sistema — token da Meta, IA — que não é por empresa). Existe para nunca deixar a operação sem acesso, mesmo com a tabela de usuários vazia.
2. **Usuário de uma empresa** (`usuarios_painel`) — `email` + senha (hash PBKDF2-HMAC-SHA256, `core/security.py`, sem dependência externa), com dois papéis:
   - `admin`: acesso completo aos dados **da própria empresa** — serviços, conhecimento, clientes, agendamentos, configurações da empresa (`/admin/empresas/{id}/editar`) e gestão de outros usuários da mesma empresa (`/admin/usuarios`).
   - `operador`: cobre o dia a dia (dashboard, clientes, agenda, atendimento, insights) mas não cria/edita/exclui serviços ou conhecimento, não gerencia usuários e não edita os dados da empresa.

A sessão guarda `is_superadmin`, `usuario_empresa_id` e `usuario_papel`. Duas dependências do FastAPI leem isso: `require_superadmin` (bloqueia tudo que não seja o login da plataforma) e `require_papel_admin` (bloqueia apenas o papel `operador`; superadmin sempre passa).

**Isolamento por empresa é aplicado uma única vez, não rota a rota.** `_query_empresa_id()` — já usada por quase toda rota de listagem/filtro para ler o `empresa_id` da query string — passou a checar primeiro a sessão: se o usuário está preso a uma empresa, o valor da sessão prevalece e o parâmetro da URL é ignorado. Isso faz o isolamento valer automaticamente em todas as rotas que já dependiam desse helper, sem precisar editar cada uma. Para rotas que carregam um recurso específico por id (`load_empresa`, `load_servico`, `load_cliente`, `load_agendamento`, `load_conhecimento`, `load_solicitacao`, `load_usuario` — todas em `admin.py`), a checagem entra no próprio loader: se o recurso pertence a outra empresa, a resposta é 404, não 403 — evita confirmar para um usuário escopado que um id de outra empresa existe.

Formulários de criação/edição não confiam no `empresa_id` submetido quando a sessão está escopada (`_empresa_id_do_formulario`): mesmo que o campo do formulário seja adulterado, o valor usado é sempre o da sessão.

## Modelo de dados

| Tabela | Papel |
|---|---|
| `empresas` | Identifica cada cliente atendido pelo bot |
| `servicos` | Serviços disponíveis por empresa |
| `clientes_finais` | Pessoas que conversaram com o bot |
| `agendamentos` | Registros criados ao final do fluxo |
| `solicitacoes_atendimento` | Pedidos de atendimento humano |
| `empresa_conhecimento` | Perguntas e respostas cadastradas por empresa |
| `conversas_iniciadas` | Log mínimo (empresa, telefone, data) usado só para métricas de conversão — o estado da conversa em si vive no Redis |
| `configuracao_sistema` | Linha única (`id=1`) com as configurações operacionais editáveis pelo painel |
| `usuarios_painel` | Login por empresa (`email` + senha com hash), com papel `admin` ou `operador` |

O isolamento é multi-tenant por coluna `empresa_id` em todas as tabelas de negócio — a mesma base atende várias empresas sem misturar dados.

## Dashboard, insights e clientes inativos

Todas as consultas agregadas ficam em `services/metricas.py` — `admin.py` só chama e renderiza, sem regra de negócio na rota.

**Dashboard** (`/admin/dashboard`) tem filtro de período (`data_inicio`/`data_fim`, padrão últimos 30 dias) e 10 indicadores: conversas iniciadas, agendamentos realizados, cancelamentos, solicitações de atendimento, taxa de conversão, serviço mais solicitado, horário mais solicitado, dia da semana mais movimentado, clientes novos e clientes recorrentes.

Duas decisões de definição, documentadas aqui porque não são óbvias só olhando o código:

- **Cliente recorrente** = dentre os que agendaram no período filtrado, quantos já tinham mais de 1 agendamento no total (histórico completo, não só do período).
- **Serviço/horário/dia mais frequentes** são calculados em Python (`collections.Counter`) a partir de uma projeção estreita, em vez de SQL agregado — de propósito: extração de hora/dia-da-semana usa funções diferentes em SQLite (testes) e Postgres (produção), e agregar em Python evita acoplar o código a um dialeto.

**Insights** (`/admin/insights`) reaproveita `calcular_metricas()` e monta frases só com números já calculados (nunca texto ou dado inventado). Se não houver dado suficiente para um insight específico, a frase correspondente simplesmente não aparece.

**Clientes inativos** (`/admin/clientes-inativos`) usa a mesma estrutura de query da listagem geral de clientes, com seletor de 30/60/90/180 dias. Um cliente é considerado inativo quando sua última interação (o agendamento mais recente, ou a data de cadastro se nunca agendou) é mais antiga que o corte selecionado.

## Compatibilidade SQLite / PostgreSQL

Os testes rodam contra SQLite (arquivo temporário por teste) e a produção contra PostgreSQL. `core/schema.py` usa `Base.metadata.create_all()` do SQLAlchemy (já portável) para criar tabelas novas, e uma migration manual idempotente (`_add_column_if_missing`) para adicionar colunas em bancos já existentes. Qualquer literal booleano usado nessa migration manual passa por `core/db_compat.sql_bool()`, que gera `TRUE`/`FALSE` — a forma que funciona nos dois bancos (SQLite aceita esses literais desde a versão 3.23; `1`/`0` funcionam no SQLite mas quebram no PostgreSQL, que exige um booleano de verdade no `DEFAULT` de uma coluna `BOOLEAN`).

## Segurança

Decisões já tomadas:

- Login do superadmin comparado com `hmac.compare_digest` (evita timing attack); senha de usuários por empresa com hash PBKDF2-HMAC-SHA256 e salt aleatório (`core/security.py`), nunca texto puro.
- Papéis e permissões por empresa (`admin`/`operador`) com isolamento de dados por `empresa_id` reforçado no carregamento de cada recurso, não só no filtro de listagem (ver seção "Autenticação e papéis no painel").
- Bancos e Redis não ficam abertos para a internet — o reverse proxy centraliza a entrada pública.
- O estado da conversa expira automaticamente no Redis.
- `ADMIN_PASSWORD` e `SESSION_SECRET_KEY` são validados na inicialização (rejeita valores fracos ou placeholders do `.env.example`).

Pontos ainda em aberto:

- Criptografia em repouso para segredos guardados no banco (token da Meta, chave de IA) — hoje ficam em texto puro.
- Validação/renovação automática de token da Meta.
- Criação do primeiro usuário de uma empresa integrada ao onboarding público — hoje precisa ser cadastrada manualmente em `/admin/usuarios`.
