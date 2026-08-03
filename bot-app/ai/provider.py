from __future__ import annotations

from abc import ABC, abstractmethod


class AIProviderError(Exception):
    """Erro genérico de provedor de IA (timeout, falha de rede, resposta inválida)."""


class AIProvider(ABC):
    """Interface única para qualquer provedor de IA (OpenAI, Claude, Gemini, Ollama...).

    Todo provider recebe mensagens no formato OpenAI-compatible (role/content) e
    devolve o texto bruto da resposta. Prompt e parsing ficam em service.py/prompts.py,
    nunca aqui — assim nenhuma lógica de negócio fica presa a um provider específico.
    """

    @abstractmethod
    async def completar(self, mensagens: list[dict[str, str]]) -> str:
        raise NotImplementedError


class OpenAIProvider(AIProvider):
    def __init__(self, api_key: str, model: str, timeout_segundos: float):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_segundos, max_retries=0)
        self._model = model

    async def completar(self, mensagens: list[dict[str, str]]) -> str:
        import openai

        try:
            resposta = await self._client.chat.completions.create(
                model=self._model,
                messages=mensagens,
                response_format={"type": "json_object"},
                temperature=0,
            )
        except openai.APIError as exc:
            raise AIProviderError(str(exc)) from exc

        conteudo = resposta.choices[0].message.content
        if not conteudo:
            raise AIProviderError("Resposta vazia do provedor de IA.")
        return conteudo
