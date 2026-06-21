"""OpenAI (ChatGPT) adapter."""

from .openai_compatible import OpenAICompatibleClient

DEFAULT_MODEL = "gpt-4o-mini"


class ChatGPTClient(OpenAICompatibleClient):
    base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"
    provider_label = "ChatGPT"

    def __init__(self, model=DEFAULT_MODEL, default_max_tokens=500):
        super().__init__(model=model, default_max_tokens=default_max_tokens)
