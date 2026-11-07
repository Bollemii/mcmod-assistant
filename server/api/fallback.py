"""Bascule vers le mode allégé quand le fournisseur principal ne répond pas.

Fournisseur principal (Gemini, avec son prompt complet) d'abord. S'il échoue (quota
atteint, panne, réseau), la question part vers le mode allégé (Ollama en local, prompt
plus court) et la réponse est marquée `degraded` : le client l'indique au joueur.

Après une panne, le principal n'est plus essayé pendant un délai, pour ne pas faire
attendre chaque joueur derrière un appel voué à l'échec :
- quota par minute : le délai indiqué par Gemini (« retry in 35s ») ;
- quota du jour épuisé : 1 heure, puis nouvel essai ;
- autre panne : ASSIST_FALLBACK_COOLDOWN_S (60 s par défaut).
"""
import logging
import re
import time
from typing import Callable, Sequence

from .llm import Answer, Answerer, LLMUnavailable
from .retrieval import ContextChunk
from .servers import ServerInfo
from .settings import Settings

log = logging.getLogger("assist")
DAILY_QUOTA_PAUSE_S = 3600


def pause_after(error: Exception, default_s: float) -> float:
    message = str(error)
    if re.search(r"PerDay|per day", message, re.IGNORECASE):
        return DAILY_QUOTA_PAUSE_S
    if m := re.search(r"retry in ([\d.]+)s", message):
        return float(m.group(1)) + 1
    return default_s


class FallbackAnswerer:
    def __init__(self, primary: Answerer, fallback: Answerer, settings: Settings,
                 clock: Callable[[], float] = time.monotonic):
        self.primary, self.fallback = primary, fallback
        self.default_pause_s = settings.fallback_cooldown_s
        self.clock = clock
        self.primary_paused_until = 0.0

    @property
    def primary_paused(self) -> bool:
        return self.clock() < self.primary_paused_until

    async def answer(self, question: str, server: ServerInfo | None, mc_version: str | None,
                     context: Sequence[ContextChunk] = ()) -> Answer:
        if not self.primary_paused:
            try:
                return await self.primary.answer(question, server, mc_version, context)
            except LLMUnavailable as e:
                pause = pause_after(e, self.default_pause_s)
                self.primary_paused_until = self.clock() + pause
                log.warning("LLM principal indisponible (%s) : mode allégé pendant %.0f s", e, pause)
        answer = await self.fallback.answer(question, server, mc_version, context)   # LLMUnavailable -> 503
        answer.degraded = True
        return answer
