"""Claude (API Anthropic). Clé : ANTHROPIC_API_KEY. Facturé à l'usage, hors abonnement Pro."""
from typing import Sequence

import anthropic

from ..llm import REFUSAL_TEXT, SYSTEM_PROMPT, Answer, LLMUnavailable, build_user_message, finish
from ..retrieval import ContextChunk
from ..servers import ServerInfo
from ..settings import Settings


class ClaudeAnswerer:
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None):
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic(timeout=settings.llm_timeout_s, max_retries=1)

    async def answer(self, question: str, server: ServerInfo | None, mc_version: str | None,
                     context: Sequence[ContextChunk] = ()) -> Answer:
        try:
            response = await self.client.beta.messages.create(
                model=self.settings.model_name,
                max_tokens=self.settings.max_tokens,
                output_config={"effort": self.settings.effort},
                # En cas de refus du modèle, l'API relance la requête sur un autre modèle.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_message(question, server, mc_version, context)}],
            )
        except anthropic.APIError as e:
            raise LLMUnavailable(str(e)) from e

        usage = response.usage
        tokens = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        if response.stop_reason == "refusal":
            return Answer(REFUSAL_TEXT, response.model, refused=True, **tokens)
        text = "\n".join(b.text.strip() for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMUnavailable(f"réponse vide (stop_reason={response.stop_reason})")
        return finish(text, response.model, **tokens)
