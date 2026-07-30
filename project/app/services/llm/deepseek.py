"""DeepSeek adapter (OpenAI Chat Completions-compatible)."""

from .openai_compatible import DEFAULT_TIMEOUT_SECONDS, OpenAICompatibleClient

DEFAULT_MODEL = "deepseek-chat"


class DeepSeekClient(OpenAICompatibleClient):
    base_url = "https://api.deepseek.com/v1"
    api_key_env = "DEEPSEEK_API_KEY"
    provider_name = "deepseek"
    provider_label = "DeepSeek"

    def __init__(
        self, model=DEFAULT_MODEL, default_max_tokens=500, timeout_s=DEFAULT_TIMEOUT_SECONDS
    ):
        super().__init__(model=model, default_max_tokens=default_max_tokens, timeout_s=timeout_s)
