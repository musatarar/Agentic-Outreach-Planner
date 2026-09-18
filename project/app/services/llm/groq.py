"""Groq adapter (OpenAI Chat Completions-compatible)"""

from .openai_compatible import DEFAULT_TIMEOUT_SECONDS, OpenAICompatibleClient

DEFAULT_MODEL = "openai/gpt-oss-20b"

# Groq bills a reasoning model's hidden reasoning against `max_tokens`, and at
# the default effort gpt-oss spends the whole copy budget on it and returns
# empty content. Low effort leaves room for the answer.
DEFAULT_REASONING_EFFORT = "low"

# Groq answers 400 for `reasoning_effort` on a model that cannot reason.
_REASONING_MODEL_PREFIXES = ("openai/gpt-oss",)


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
        reasoning_effort=DEFAULT_REASONING_EFFORT,
    ):
        super().__init__(
            model=model,
            default_max_tokens=default_max_tokens,
            api_key=api_key,
            timeout_s=timeout_s,
        )
        # None means send nothing and take the model's own default.
        self.reasoning_effort = reasoning_effort

    def _request_extras(self) -> dict[str, object]:
        if self.reasoning_effort and self.model.startswith(_REASONING_MODEL_PREFIXES):
            return {"reasoning_effort": self.reasoning_effort}
        return {}
