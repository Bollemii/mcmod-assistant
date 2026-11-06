"""Toute API compatible OpenAI (/v1/chat/completions).

Exemples de configuration :
- Ollama en local, gratuit : ASSIST_OPENAI_BASE_URL=http://127.0.0.1:11434/v1, ASSIST_MODEL=<modèle installé>
- Mistral, Groq, OpenRouter… : leur URL d'API, ASSIST_OPENAI_API_KEY, ASSIST_MODEL
"""
import httpx

from ..llm import REFUSAL_TEXT, SYSTEM_PROMPT, Answer, LLMUnavailable, build_user_message
from ..servers import ServerInfo
from ..settings import Settings


class OpenAICompatAnswerer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.openai_base_url:
            raise ValueError("ASSIST_OPENAI_BASE_URL est obligatoire avec ASSIST_LLM=openai")
        self.settings = settings
        headers = {"Authorization": f"Bearer {settings.openai_api_key}"} if settings.openai_api_key else {}
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout_s, headers=headers)
        self.url = settings.openai_base_url.rstrip("/") + "/chat/completions"

    async def answer(self, question: str, server: ServerInfo | None, mc_version: str | None) -> Answer:
        model = self.settings.model_name
        payload = {
            "model": model,
            "max_tokens": self.settings.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(question, server, mc_version)},
            ],
        }
        try:
            response = await self.client.post(self.url, json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMUnavailable(f"{self.url} : {e}") from e

        usage = data.get("usage") or {}
        tokens = {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "content_filter":
            return Answer(REFUSAL_TEXT, model, refused=True, **tokens)
        text = ((choice.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMUnavailable(f"réponse vide (finish_reason={choice.get('finish_reason')})")
        return Answer(text, data.get("model") or model, **tokens)
