# Projeto

Este projeto é um SaaS de atendimento e pré-agendamento via WhatsApp.

Objetivo principal:

Conseguir os primeiros clientes pagantes em até 30 dias.

Sempre priorize funcionalidades visíveis para o cliente.

---

## Arquitetura

Backend:

- FastAPI

Banco:

- PostgreSQL

Cache:

- Redis

Integração:

- Evolution API

Mensagens interativas:

- Meta Graph API

---

## Regras

Antes de escrever código:

- leia os módulos relacionados;
- reutilize código existente;
- evite duplicação;
- não faça overengineering.

---

## Sempre executar

Ao concluir qualquer tarefa:

- revisar código;
- procurar bugs;
- escrever testes;
- atualizar documentação;
- verificar impacto em outras funcionalidades.

---

## Nunca

Nunca criar:

- abstrações desnecessárias;
- classes sem necessidade;
- serviços genéricos;
- múltiplas camadas sem benefício.

---

## Prioridade

Sempre priorize:

1 Valor percebido pelo cliente

2 Simplicidade

3 Robustez

4 Performance

---

## Objetivo

Todo código produzido deve estar pronto para produção.

Não gerar código de exemplo.

Não deixar TODOs.

Não deixar comentários dizendo "implementar depois".

Sempre entregar funcionalidade completa.

#sprints
1. Painel Administrativo
2. CRUD de Empresas
3. crud de serviços;
4. Onboarding
5. Lembretes automáticos de agendamento via WhatsApp (reduz falta/no-show)
11. Insights e Conhecimento da Empresa (base de conhecimento por empresa usada pela IA, dashboard com indicadores por período, insights automáticos, clientes inativos)
