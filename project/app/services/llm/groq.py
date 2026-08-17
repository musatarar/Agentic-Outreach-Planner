"""Groq adapter (OpenAI Chat Completions-compatible)"""

from .openai_compatible import DEFAULT_TIMEOUT_SECONDS, OpenAICompatibleClient

DEFAULT_MODEL = "openai/gpt-oss-20b"


class GroqClient(OpenAICompatibleClient):
    base_url = "https://api.groq.com/openai/v1"
    api_key_env = "GROQ_API_KEY"
    provider_name = "groq"
    provider_label = "Groq"

    def __init__(
        self,
        model=DEFAULT_MODEL,
        default_max_tokens=500,
        api_key=None,
        timeout_s=DEFAULT_TIMEOUT_SECONDS,
    ):
        super().__init__(
            model=model,
            default_max_tokens=default_max_tokens,
            api_key=api_key,
            timeout_s=timeout_s,
        )
