"""LLM local servi par Ollama (docker-compose.yml), sur processeur : le mode allégé.

API native d'Ollama (/api/chat), et non son API compatible OpenAI, car elle seule
permet de régler :
- num_thread : sous Docker, les 14 threads par défaut se gênent (3 à 4 fois plus lent) ;
- num_ctx : le contexte par défaut d'Ollama tronquerait le prompt ;
- think : désactive le raisonnement des modèles qui en ont un (sinon réponse en anglais).

Sur processeur, le temps de réponse dépend surtout de la longueur du prompt (~70 tokens
lus par seconde avec gemma3:4b) : ce fournisseur utilise donc le prompt allégé
(LIGHT_SYSTEM_PROMPT), moins d'extraits et des extraits plus courts. Mesuré le
07/11/2026 : ~20 à 30 s par réponse, 9 réponses justes sur 10.
"""
from typing import Sequence

import httpx

from ..llm import LIGHT_SYSTEM_PROMPT, Answer, LLMUnavailable, build_user_message, finish
from ..retrieval import ContextChunk
from ..servers import ServerInfo
from ..settings import Settings


class OllamaAnswerer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        # ASSIST_MODEL ne vaut que si Ollama est le fournisseur principal ; sinon ASSIST_OLLAMA_MODEL.
        self.model = (settings.model if settings.llm == "ollama" and settings.model else settings.ollama_model)
        self.url = settings.ollama_url.rstrip("/").removesuffix("/v1") + "/api/chat"
        self.client = client or httpx.AsyncClient(timeout=settings.ollama_timeout_s)

    def request(self, question: str, server: ServerInfo | None, mc_version: str | None,
                context: Sequence[ContextChunk]) -> dict:
        light = list(context)[:self.settings.light_top_k]
        message = build_user_message(question, server, mc_version, light, self.settings.light_extract_chars)
        options = {"num_ctx": 4096, "temperature": 0.2, "num_predict": 300}
        if self.settings.ollama_threads:
            options["num_thread"] = self.settings.ollama_threads
        return {"model": self.model, "stream": False, "think": False, "options": options,
                "messages": [{"role": "system", "content": LIGHT_SYSTEM_PROMPT},
                             {"role": "user", "content": message}]}

    async def answer(self, question: str, server: ServerInfo | None, mc_version: str | None,
                     context: Sequence[ContextChunk] = ()) -> Answer:
        try:
            response = await self.client.post(self.url, json=self.request(question, server, mc_version, context))
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMUnavailable(f"Ollama ({self.model}) : {e!r}") from e
        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMUnavailable(f"Ollama ({self.model}) : réponse vide")
        return finish(text, self.model, input_tokens=data.get("prompt_eval_count") or 0,
                      output_tokens=data.get("eval_count") or 0)
