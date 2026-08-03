# Security Review

Sempre analise:

- autenticação
- autorização
- SQL Injection
- XSS
- CSRF
- Secrets
- Environment Variables
- Logs
- Dados sensíveis

Nunca permita:

- senhas hardcoded
- tokens no código
- debug ligado em produção
- exceções silenciosas

Toda implementação deve seguir o princípio do menor privilégio.