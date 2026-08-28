# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## [Unreleased]

### Corrigido (auditoria de segurança)

- Rate limiting simples (Redis, contador com expiração) em `POST /admin/login` (10/min), `POST /admin/esqueci-senha` (5/min) e `POST /onboarding` (5/min) por IP — antes não havia nenhum limite, permitindo força bruta de credenciais e flood de criação de conta/e-mail. Falha do Redis nunca bloqueia o login (`core/rate_limit.py::excedeu_limite` deixa passar e loga, mesmo princípio de `services.configuracoes.obter_configuracao_isolada`).
- `core/redis_client.py`: cliente Redis compartilhado ganhou timeout explícito de 2s (`socket_connect_timeout`/`socket_timeout`) — sem isso, um Redis indisponível travava a requisição pelo timeout padrão do SO em vez de falhar rápido.
- `/docs`, `/redoc` e `/openapi.json` desligados nos dois apps (`main.py` e `admin.py`) — estavam públicos por padrão do FastAPI, expondo toda a superfície de rotas (inclusive administrativas) sem autenticação.
- Cookie de sessão agora exige HTTPS (`https_only=True` em vez de `False`, que estava setado explicitamente) — antes trafegava em texto puro se o app fosse acessado por HTTP.
- **Achado tratado fora do código, direto na infraestrutura**: o Caddy de produção tinha um segundo domínio (hostname automático da VPS) roteando direto para a porta da Evolution API (8080), expondo a API de gerenciamento de instâncias WhatsApp de todas as empresas publicamente, sem passar pelo FastAPI nem pelo `WEBHOOK_SECRET`. Bloco removido do Caddyfile do servidor.
- **Identificado, não corrigido nesta rodada**: `WEBHOOK_SECRET` é um segredo único compartilhado por todas as instâncias da Evolution API — quem o obtém pode forjar mensagem/cancelamento em nome de qualquer empresa (o nome da instância é previsível, é o próprio slug). Requer token por empresa; decisão de rollout pendente porque instâncias já existentes têm a URL antiga embutida na Evolution API.

### Adicionado (lembrete de agendamento por e-mail)

- Cada empresa agora escolhe em `/admin/configurar-bot/lembretes` se quer lembrete automático por e-mail (`Empresa.lembrete_canal_email`). Agendamento grava `Agendamento.lembrete_email_enviado_em` (novo) ao enviar com sucesso; se o cliente não tiver e-mail cadastrado, o ciclo fecha sem tentar de novo a cada rodada do loop de lembretes.
- E-mail via API do [Resend](https://resend.com) (`integrations/email_client.py`, reescrito — antes era SMTP genérico via `smtplib`, usado só na recuperação de senha e nunca ligado a lembretes; migrado para não manter dois mecanismos de envio de e-mail no projeto). Credenciais (`RESEND_API_KEY`/`EMAIL_FROM_ENDERECO`/`EMAIL_FROM_NOME`) são globais, mesmo padrão das credenciais da Meta — nenhuma empresa precisa configurar servidor de e-mail próprio.
- `ClienteFinal.email` (novo campo, opcional) — editável pelo painel em `/admin/clientes/{id}` e no cadastro manual de cliente; a conversa por WhatsApp não coleta e-mail automaticamente, pra não adicionar fricção ao fluxo de agendamento.
- `.env.example`: variáveis `SMTP_*` substituídas por `RESEND_API_KEY`/`EMAIL_FROM_ENDERECO`/`EMAIL_FROM_NOME`.

**Nota:** a primeira versão desta feature também incluía lembrete por WhatsApp via template pré-aprovado da Meta, com fallback automático pro WhatsApp quando o cliente não tinha e-mail. Removido antes de sair do `[Unreleased]` — não vale amarrar a funcionalidade a uma aprovação de template pela Meta no estágio atual do produto. Pode voltar como canal adicional mais adiante; código no histórico do git.

### Alterado (copywriting da landing page)

- Reescrita completa dos textos de `templates/site/landing.html` (hero, seção de identificação com o problema — nova —, "como funciona", "o que o bot faz", FAQ e CTA final). Antes de mexer em qualquer texto, foi feito um levantamento das funcionalidades realmente implementadas (README, `docs/architecture.md`, `conversa.py`, `services/agenda.py`) para garantir que cada afirmação da página corresponde a um comportamento real do sistema — inclusive a remoção da frase "Só o que já está implementado — sem promessa vazia" (uma nota interna que vazou pro texto público) e a descoberta de que `_empresa_horario_disponivel()` em `conversa.py` está morta (nunca chamada) — por isso a página não afirma que o bot "responde de acordo com o horário configurado" fora do que já é real (validação de horário/almoço/dias indisponíveis na hora de marcar um agendamento, isso sim implementado em `services/agenda.py`).

### Corrigido (auditoria da reformulação de onboarding)

- `POST /admin/empresas/cadastrar`: se a linha do usuário na sessão não existisse mais no banco, `vincular_empresa` estourava `AttributeError` (500) **depois** de já ter criado a instância na Evolution API e a `Empresa` — deixando os dois órfãos. Usuário agora é buscado e validado antes de qualquer criação.
- `POST /onboarding`: corrida entre duas contas cadastradas com o mesmo e-mail ao mesmo tempo (a pre-checagem não é atômica com o `commit`) resultava em 500 em vez da mensagem "já existe uma conta com esse e-mail". Agora captura `IntegrityError`.
- Empresas que já existiam (e já estavam `ativo=True`) antes da migration de `ativado_em` ficavam com esse campo nulo — na primeira pausa, o painel mostraria "nunca configurado" em vez de "pausado". Migration agora faz backfill (`ativado_em = criado_em` para quem já estava ativo).
- `/admin/dashboard` não tinha nenhuma indicação de configuração pendente pra quem já tem empresa (só o hub isolado tinha) — reaproveitada a mesma lógica de status (`_status_configuracao_bot`) num card no topo do dashboard.
- Hub `/admin/configurar-bot` mostrava a mesma mensagem ("Tudo pronto pra ativar") tanto pra quem nunca ativou quanto pra quem só pausou — agora distingue usando `ativado_em`, já existente mas não aproveitado nessa tela.

### Alterado

- **Reformulação completa da jornada de onboarding, landing page e ativação do bot.** `/` deixou de redirecionar pra login/onboarding e virou uma landing page pública de verdade (hero, como funciona, benefícios reais, FAQ, CTAs separados de "criar conta"/"entrar"). `/onboarding` deixou de cadastrar empresa — agora só cria a conta da pessoa (nome, e-mail, senha); a empresa é cadastrada depois, já logada, em `/admin/empresas/cadastrar` (novo). `usuarios_painel.empresa_id` passou a ser opcional (migration `ALTER COLUMN ... DROP NOT NULL` em Postgres) — um usuário pode existir sem empresa vinculada; nesse estado, `/admin/dashboard` mostra um CTA único ("Cadastrar minha empresa") e qualquer outra rota do painel redireciona de volta pra lá (guard centralizado em `require_admin`, sem precisar editar rota por rota). Empresas passam a nascer com `ativo=False` — **conectar o WhatsApp não ativa o bot sozinho** — e um novo hub `/admin/configurar-bot` mostra o checklist real (mensagens do bot, serviços, horários) até tudo estar pronto pra apertar "Ativar bot" (`POST /admin/empresas/{id}/ativar`/`/pausar`, acessível também ao admin da própria empresa, não só ao superadmin). Novo campo `Empresa.ativado_em` distingue "nunca configurado" de "pausado depois de já ter estado no ar". Removidas as rotas/templates antigos `/onboarding/configurar`, `/onboarding/conectar*` e `/onboarding/sucesso` — o fluxo de QR code passou a reaproveitar só `/admin/empresas/{id}/conectar`, já existente, eliminando a duplicação entre as duas versões paralelas de conectar o WhatsApp.
- Instruções passo a passo (abrir WhatsApp → Dispositivos conectados → Conectar dispositivo → escanear), tratamento de expiração do QR code (~55s, com botão pra gerar um novo) e erro visível quando "gerar novo código" falha (antes falhava em silêncio) em `templates/partials/qrcode_connect.html`.

### Adicionado

- Slug da empresa é gerado automaticamente a partir do nome digitado (JS no onboarding público e em `/admin/empresas/nova`) — remove acentos, espaços e símbolos, convertendo para o formato exigido (`a-z0-9-`) sem o usuário precisar entender a regra. Se o slug for editado manualmente, o preenchimento automático para de sobrescrever. Quando um slug inválido ainda chega ao servidor, a mensagem de erro agora sugere um valor pronto para usar.
- Checagem prévia de instância já existente na Evolution API antes de `criar_instancia` (`instancia_existe`, novo helper em `integrations/evolution_client.py`): antes, um slug que já tinha uma instância associada (ex.: sobra de uma tentativa anterior) fazia a Evolution API recusar com "Forbidden", sem explicar o motivo. Agora a mensagem é direta: "Já existe uma instância de WhatsApp com o identificador '...'".
- Placeholders e textos de ajuda em campos de formato não óbvio que faltavam (ex.: telefone WhatsApp em `/admin/empresas/nova`, que já existiam no onboarding público).
- Recuperação de senha self-service para usuários do painel (`/admin/esqueci-senha` + `/admin/redefinir-senha`): token de uso único com validade de 1 hora, guardado como hash (nunca em texto puro), enviado por e-mail via SMTP genérico (novo módulo `integrations/email_client.py`, usa só `smtplib` da stdlib — funciona com Gmail, SendGrid, Mailgun, SES etc.). A resposta do pedido de redefinição é sempre a mesma mensagem, exista ou não o e-mail, para não vazar quais usuários têm cadastro. Novas variáveis opcionais `SMTP_*` no `.env` — sem configurar, a tela continua funcionando e só não envia o e-mail (fica registrado no log). Superadmin (login via `.env`) não passa por esse fluxo.
- Onboarding público cria o usuário `admin` da empresa automaticamente (nome, e-mail e senha coletados no passo 2, junto com a operação) e já autentica a sessão no painel ao final — não depende mais de um superadmin cadastrar esse usuário manualmente em `/admin/usuarios` depois. E-mail duplicado é validado antes mesmo de chamar a Evolution API.
- Conexão automática do WhatsApp via Evolution API: o onboarding público (`/onboarding`) agora cria e conecta a instância sozinho (QR code + código de pareamento exibidos na hora, com polling até a conexão abrir), sem precisar mais criar a instância manualmente fora do sistema. Nova ação `/admin/empresas/{id}/conectar` no painel para reconectar o WhatsApp de uma empresa já existente (sessão caiu, celular trocado etc.). Novo módulo `integrations/evolution_client.py`. Requer a variável de ambiente `PUBLIC_BASE_URL` (nova, obrigatória).
- Papéis e permissões no painel administrativo: cada empresa cliente pode ter usuários próprios (tabela `usuarios_painel`, senha com hash PBKDF2), com papel `admin` (acesso completo à própria empresa, inclusive gestão de outros usuários) ou `operador` (dia a dia — agenda, clientes, atendimento — sem exclusões, configurações ou gestão de usuários). O login único do `.env` (`ADMIN_USERNAME`/`ADMIN_PASSWORD`) passa a ser o superadmin de bootstrap, com acesso a todas as empresas. Nova tela `/admin/usuarios`; `/admin/empresas` e `/admin/configuracoes` (config global do sistema) ficam restritas ao superadmin. Corrige de passagem dois pontos que confiavam sem validar em `empresa_id` vindo de query/form nas rotas de atualização de status de agendamento e de solicitação de atendimento.
- Cadastro manual de cliente final direto pelo painel (`/admin/clientes/novo`), sem depender do cliente iniciar a conversa pelo WhatsApp. Telefone é normalizado (apenas dígitos) e validado contra duplicidade por empresa.

### Corrigido

- Cadastro de empresa (onboarding público e `/admin/empresas/nova`) mostrava sempre a mesma mensagem genérica quando a conexão com o WhatsApp falhava, escondendo a causa real (Evolution API fora do ar vs. recusa da API por número já em uso, chave inválida etc.). Nova exceção `EvolutionAPIConexaoError` separa falha de rede (mensagem genérica, pedir para tentar de novo) de recusa da API (mostra o motivo real devolvido pela Evolution). Conflito de slug na gravação também passou a ser logado (antes falhava silenciosamente nos logs).
- `tests/test_lembretes.py::test_reagendamento_reseta_lembrete_enviado_em` usava uma data hardcoded que expirou; passou a calcular a próxima segunda-feira dinamicamente.

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
