# Funcionalidades implementadas

Este documento lista só o que já está implementado e funcionando, do mais básico ao mais avançado. Para arquitetura, stack técnica e como rodar o projeto, ver `readme.md`.

## 1. Infraestrutura básica

- Docker Compose sobe PostgreSQL, Redis e Evolution API.
- Bot roda em container próprio, supervisionado por systemd na VPS.
- Rotas de saúde `/healthz` (liveness) e `/readyz` (valida conexão com banco e Redis).
- Configuração 100% via variáveis de ambiente (Pydantic Settings) — nenhuma credencial no código.
- Bootstrap automático de schema: cria tabelas novas e adiciona colunas em bancos já existentes, sem framework de migration.

## 2. Multi-tenant

- Isolamento por `empresa_id` em todas as tabelas de negócio (empresas, serviços, clientes, agendamentos, solicitações).
- Identificação automática da empresa a partir da instância recebida no webhook da Evolution API.
- A mesma base de código atende várias empresas ao mesmo tempo, sem misturar dados.

## 3. Onboarding e cadastro

- Onboarding público (`/onboarding`): cadastra a empresa, configura o WhatsApp e cria o primeiro serviço sem precisar de terminal.
- Cadastro e edição de empresas pelo painel (nome, slug, segmento, horário de funcionamento, horário de almoço, dias indisponíveis, mensagens personalizadas de boas-vindas/confirmação/encerramento).
- Cadastro e edição de serviços (nome, duração, preço, ordem de exibição, ativar/desativar, exclusão lógica).

## 4. Painel administrativo (`/admin`)

- Login com sessão, usuário e senha vindos de variável de ambiente.
- Dashboard com métricas: total de empresas, clientes, serviços, agendamentos do dia e solicitações de atendimento pendentes, com filtro por empresa.
- Listagem de agendamentos com filtros por empresa, status, data, serviço e cliente.
- Painel de clientes: busca por nome/telefone, ordenação (mais recente, nome, mais agendamentos, último atendimento) e ficha de detalhe com histórico completo de agendamentos e de solicitações de atendimento do cliente.

## 5. Fluxo de conversa (máquina de estados)

- Ativação por palavra-chave (`oibot`, `oi`, `bom dia` etc.) ou por clique em botão/lista.
- Lista de serviços ativos enviada como mensagem interativa via Meta Graph API.
- Escolha de período (manhã/tarde) ou horário livre digitado em texto.
- Confirmação de agendamento com criação automática do cliente final, se ainda não existir.
- Atalhos globais — `menu`, `voltar`, `cancelar` — funcionam em qualquer ponto da conversa, não só no passo esperado.
- Respostas de fallback contextuais: a mensagem de erro muda de acordo com o passo atual da conversa; nunca silencia o cliente.

## 6. Motor de agendamento

- Validação completa antes de confirmar: empresa ativa, serviço ativo, dia disponível, horário de funcionamento, conflito com outro agendamento.
- Reagendamento e cancelamento, ambos com confirmação antes de executar.
- Sugestão automática de horários alternativos quando o horário pedido não está disponível.
- Bloqueio por dias da semana indisponíveis, datas específicas indisponíveis e intervalo mínimo configurável entre atendimentos.

## 7. Atendimento humano

- Registro de solicitação de atendimento humano diretamente pela conversa do bot.
- Prevenção de duplicidade: o mesmo telefone na mesma empresa não abre duas solicitações pendentes ao mesmo tempo.
- Fila de solicitações no painel administrativo, com mudança de status (pendente → em atendimento → finalizado).

## 8. Lembretes automáticos de agendamento

- Ciclo assíncrono em segundo plano, dentro do próprio processo do bot — sem serviço externo, sem dependência nova.
- Envia lembrete via WhatsApp (template pré-aprovado da Meta) dentro de uma janela configurável de horas antes do compromisso.
- Controle de envio único por agendamento; reagendar libera um novo lembrete para a nova data.
- Indicador visual no painel — ficha do cliente mostra se o lembrete já foi enviado.

## 9. Base de conhecimento da empresa

- Cada empresa tem suas próprias perguntas e respostas cadastradas (`/admin/conhecimento`), com CRUD completo: criar, editar, ativar/desativar, excluir logicamente.
- Consultada automaticamente antes da IA sempre que a máquina de estados não sabe o que fazer com uma mensagem — se encontrar uma pergunta cadastrada relevante, responde direto com o texto cadastrado, sem gastar chamada de IA nem risco de inventar informação.
- Matching por sobreposição de palavras (tolera pequenas variações como "aceita"/"aceitam"), com limiar de confiança — só responde quando tem certeza razoável, senão segue para a IA ou para o fallback padrão.

## 10. Camada de IA / interpretação de linguagem natural

- Entra só como último recurso — depois da base de conhecimento — quando a máquina de estados não consegue interpretar a mensagem. Não substitui nenhum fluxo existente.
- Reconhece 9 intenções (agendar, cancelar, reagendar, consultar horários, consultar serviços, consultar preços, falar com atendente, saudação, desconhecido) e extrai entidades (serviço, data, horário, período, nome, telefone).
- Provider de IA plugável por trás de uma interface única, hoje implementado para OpenAI e já preparado para outros provedores (Claude, Gemini, Ollama).
- Cache de interpretação no Redis por empresa, timeout configurável e fallback seguro: qualquer falha da IA (timeout, erro do provedor, resposta inválida) mantém o fluxo normal funcionando sem interrupção.
- Desligada por padrão — só entra em ação se for explicitamente habilitada e configurada com uma chave de API.

## 11. Dashboard avançado, insights e clientes inativos

- Dashboard com filtro de período (data inicial/final) e 10 indicadores: conversas iniciadas, agendamentos realizados, cancelamentos, solicitações de atendimento, taxa de conversão, serviço mais solicitado, horário mais solicitado, dia da semana mais movimentado, clientes novos e clientes recorrentes — todos filtráveis por empresa.
- Página de Insights com frases geradas automaticamente a partir dos dados reais (ex.: "O serviço X representa 42% dos agendamentos", "18 clientes não retornam há mais de 90 dias") — nunca um número inventado; se não há dado suficiente, o insight simplesmente não aparece.
- Listagem de clientes inativos com corte configurável (30/60/90/180 dias), mostrando nome, telefone, último atendimento e quantidade de agendamentos — pensada para virar base de campanhas de retorno no futuro.

## 12. Configurações operacionais pelo painel

- Credenciais da Meta Graph API, palavra de ativação do bot, parâmetros de lembrete automático e configuração completa da camada de IA — tudo editável em `/admin/configuracoes`, sem precisar de acesso ao servidor.
- Mudança feita no painel vale na próxima mensagem ou no próximo ciclo de lembrete, sem precisar reiniciar o bot.
- Campos sensíveis (token da Meta, chave de IA) nunca aparecem de volta na tela — só é possível substituir, não visualizar o valor salvo.
