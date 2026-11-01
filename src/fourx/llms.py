"""The three LLMs the agent can use, and one function to get any of them by name."""
from dataclasses import dataclass

from .providers import ChatAdapter, GeminiAdapter, ProviderError, make_adapter


@dataclass(frozen=True)
class LLMSpec:
    name: str
    company: str
    api_key_env: str  # where the key is read from, in .env
    model_env: str    # where the model name is read from, in .env


LLMS = {
    "openai": LLMSpec("openai", "OpenAI", "OPENAI_API_KEY", "OPENAI_MODEL"),
    "gemini": LLMSpec("gemini", "Google Gemini", "GEMINI_API_KEY", "GEMINI_MODEL"),
    "groq": LLMSpec("groq", "Groq", "GROQ_API_KEY", "GROQ_MODEL"),
}


def get_llm(name: str) -> GeminiAdapter | ChatAdapter:
    """Return the ready-to-use LLM for "openai", "gemini" or "groq"; it has .complete(...)."""
    key = name.strip().casefold()
    if key not in LLMS:
        raise ProviderError(f"Unknown LLM {name!r}. Choose one of: {', '.join(LLMS)}.")
    return make_adapter(key)
