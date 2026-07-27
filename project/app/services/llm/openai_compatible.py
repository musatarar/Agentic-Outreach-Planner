"""Shared adapter for any OpenAI Chat Completions-compatible provider.

OpenAI (ChatGPT), DeepSeek, and Groq all expose the same
``POST {base_url}/chat/completions`` wire format, so one ``httpx``-based client
covers all three. Subclasses set ``base_url``, ``api_key_env``, and a default
``model``. The API key comes from the explicit ``api_key`` passed in by the
factory (:mod:`project.app.services.llm`); ``api_key_env`` is read directly
only as a fallback when ``api_key`` is ``None``.
"""

import os

import httpx

from .base import LLMClient

_TIMEOUT_SECONDS = 60.0


class OpenAICompatibleClient(LLMClient):
    # Subclasses must override these.
    base_url: str
    api_key_env: str
    provider_label = "OpenAI-compatible"

    def complete(self, prompt, max_tokens=None, timeout=None):
        api_key = self.api_key or os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"{self.provider_label} provider selected but {self.api_key_env} "
                f"is not set. Add it to your .env file."
            )

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": max_tokens or self.default_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout if timeout is not None else _TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
