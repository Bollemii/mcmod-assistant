"""Limite de débit en mémoire : N questions par joueur sur une fenêtre glissante."""
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: int, clock=time.monotonic):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> float:
        """Enregistre une question ; renvoie 0 si elle est acceptée, sinon l'attente en secondes."""
        now = self.clock()
        hits = self.hits[key]
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return hits[0] + self.window_s - now
        hits.append(now)
        return 0.0
