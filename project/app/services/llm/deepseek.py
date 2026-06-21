"""DeepSeek adapter (OpenAI Chat Completions-compatible)."""

from .openai_compatible import OpenAICompatibleClient

DEFAULT_MODEL = "deepseek-chat"


class DeepSeekClient(OpenAICompatibleClient):
    base_url = "https://api.deepseek.com/v1"
    api_key_env = "DEEPSEEK_API_KEY"
    provider_label = "DeepSeek"

    def __init__(self, model=DEFAULT_MODEL, default_max_tokens=500):
        super().__init__(model=model, default_max_tokens=default_max_tokens)
