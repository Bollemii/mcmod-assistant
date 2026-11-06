"""Calcul des embeddings (vecteurs) des chunks et des questions.

Fournisseur choisi par ASSIST_EMBED :
- "ollama" (défaut) : bge-m3 servi par Ollama en local (docker-compose.yml), multilingue,
  1024 dimensions, sans quota ni coût. Adresse : ASSIST_EMBED_URL.
- "gemini" : gemini-embedding-2, multilingue, gratuit. Palier gratuit, par
  modèle : 100 textes et 30 000 tokens par minute, et **1 000 textes par jour**. Le
  premier chargement du wiki (~950 chunks) prend une quinzaine de minutes et presque
  tout le quota du jour ; ensuite, seuls les chunks modifiés sont recalculés, et chaque
  question en consomme un. Quota épuisé : la recherche passe en plein texte seul.
- "openai" : une autre API compatible OpenAI (/v1/embeddings), à l'adresse ASSIST_EMBED_URL.
- "fake" : vecteurs déterministes tirés du texte, pour les tests.

Les appels sont synchrones : l'API les lance dans un thread (asyncio.to_thread).
"""
import hashlib
import logging
import math
import re
import time
from typing import Protocol

from .settings import Settings

log = logging.getLogger("assist")


class EmbeddingUnavailable(Exception):
    """Le service d'embeddings n'a pas répondu (quota, réseau) ; la recherche passe en plein texte seul."""


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str, max_wait_s: float = 5) -> list[float]: ...


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class MinuteBudget:
    """Respecte deux limites glissantes sur 60 s : nombre de textes et nombre de tokens."""

    def __init__(self, texts_per_min: int, tokens_per_min: int, clock=time.monotonic, sleep=time.sleep):
        self.max_texts, self.max_tokens = texts_per_min, tokens_per_min
        self.clock, self.sleep = clock, sleep
        self.history: list[tuple[float, int, int]] = []    # (instant, textes, tokens)

    def wait_for(self, texts: int, tokens: int) -> None:
        while True:
            now = self.clock()
            self.history = [h for h in self.history if now - h[0] < 60]
            used_texts = sum(h[1] for h in self.history)
            used_tokens = sum(h[2] for h in self.history)
            if not self.history or (used_texts + texts <= self.max_texts and used_tokens + tokens <= self.max_tokens):
                self.history.append((now, texts, tokens))
                return
            self.sleep(max(0.5, 60 - (now - self.history[0][0])))


def estimate_tokens(text: str) -> int:
    """Estimation prudente, mesurée sur le wiki Excalia : ~2,6 caractères par token en
    français, mais les caractères hors alphabet latin (petites capitales « ᴄᴀᴏᴜᴛᴄʜᴏᴜᴄ »
    des recettes, symboles ★) coûtent souvent un token chacun."""
    exotic = sum(1 for ch in text if ord(ch) > 0x24F)
    return int(len(text) / 2.5) + exotic + 1


class _BatchTooLarge(Exception):
    """Le lot dépasse le quota de tokens par minute : il sera coupé en deux."""


class GeminiEmbedder:
    # Palier gratuit, par modèle : 100 textes et 30 000 tokens par minute (on garde une marge).
    TEXTS_PER_MIN = 95
    TOKENS_PER_MIN = 27_000
    MAX_BATCH = 100

    def __init__(self, settings: Settings, client=None, budget: MinuteBudget | None = None):
        self.model = settings.embed_model_name
        self.dim = settings.embed_dim
        self._client = client
        self.budget = budget or MinuteBudget(self.TEXTS_PER_MIN, self.TOKENS_PER_MIN)

    @property
    def client(self):
        if self._client is None:
            from google import genai
            try:
                self._client = genai.Client()
            except ValueError as e:
                raise EmbeddingUnavailable(f"client Gemini impossible à créer (GEMINI_API_KEY ?) : {e}") from e
        return self._client

    def _call(self, texts: list[str], task: str, max_wait_s: float, splittable: bool = False) -> list[list[float]]:
        from google.genai import errors, types
        contents = [types.Content(parts=[types.Part(text=t)]) for t in texts]
        config = types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim)
        waited = 0.0
        while True:
            try:
                r = self.client.models.embed_content(model=self.model, contents=contents, config=config)
                # Gemini ne normalise les vecteurs qu'à 3072 dimensions : on le fait nous-mêmes.
                return [_normalize(e.values) for e in r.embeddings]
            except errors.APIError as e:
                if e.code == 429 and splittable and "TokensPerMinute" in str(e):
                    raise _BatchTooLarge() from e
                if e.code == 429 and "PerDay" in str(e):     # quota du jour épuisé : inutile d'attendre
                    raise EmbeddingUnavailable(f"quota journalier d'embeddings atteint ({self.model})") from e
                m = re.search(r"retry in ([\d.]+)s", str(e))
                wait = float(m.group(1)) + 1 if m else 20.0
                if e.code != 429 or waited + wait > max_wait_s:
                    raise EmbeddingUnavailable(f"Gemini {e.code} : {e.message}") from e
                log.warning("Quota d'embeddings atteint, attente de %.0f s", wait)
                time.sleep(wait)
                waited += wait
            except Exception as e:   # réseau
                raise EmbeddingUnavailable(str(e)) from e

    def batches(self, texts: list[str]) -> list[list[str]]:
        """Lots qui tiennent chacun dans le budget d'une minute."""
        out, current, tokens = [], [], 0
        for t in texts:
            n = estimate_tokens(t)
            if current and (len(current) >= self.MAX_BATCH or tokens + n > self.TOKENS_PER_MIN):
                out.append(current)
                current, tokens = [], 0
            current.append(t)
            tokens += n
        return out + ([current] if current else [])

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors, queue = [], self.batches(texts)
        while queue:
            batch = queue.pop(0)
            # Le budget compte aussi les essais refusés : après un refus, on attend la minute suivante.
            self.budget.wait_for(len(batch), sum(estimate_tokens(t) for t in batch))
            try:
                vectors += self._call(batch, "RETRIEVAL_DOCUMENT", max_wait_s=600, splittable=len(batch) > 1)
            except _BatchTooLarge:
                half = len(batch) // 2
                queue[:0] = [batch[:half], batch[half:]]
                log.warning("Lot de %d textes trop gros pour le quota de tokens : coupé en deux", len(batch))
                continue
            log.info("Embeddings : %d/%d", len(vectors), len(texts))
        return vectors

    def embed_query(self, text: str, max_wait_s: float = 5) -> list[float]:
        # Pas d'attente du budget ici : une question ne doit pas patienter derrière un chargement.
        return self._call([text], "RETRIEVAL_QUERY", max_wait_s)[0]


class OpenAICompatEmbedder:
    # Petits lots : sur processeur (sans GPU), bge-m3 met ~1 à 2 s par extrait long.
    BATCH = 8
    DOCUMENT_TIMEOUT_S = 600
    QUERY_TIMEOUT_S = 30

    def __init__(self, settings: Settings):
        import httpx
        self.model = settings.embed_model_name
        self.dim = settings.embed_dim
        # Clé seulement pour une API distante (ASSIST_EMBED=openai) ; Ollama n'en demande pas.
        key = settings.openai_api_key if settings.embed == "openai" else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = httpx.Client(base_url=settings.embed_url, headers=headers)

    def _call(self, texts: list[str], timeout_s: float) -> list[list[float]]:
        try:
            r = self.http.post("/embeddings", json={"model": self.model, "input": texts}, timeout=timeout_s)
            r.raise_for_status()
        except Exception as e:
            raise EmbeddingUnavailable(str(e)) from e
        vectors = [_normalize(d["embedding"]) for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        if vectors and len(vectors[0]) != self.dim:
            raise ValueError(f"{self.model} donne {len(vectors[0])} dimensions, la base en attend {self.dim}")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for i in range(0, len(texts), self.BATCH):
            vectors += self._call(texts[i:i + self.BATCH], self.DOCUMENT_TIMEOUT_S)
            if len(vectors) % 80 == 0 or len(vectors) == len(texts):
                log.info("Embeddings : %d/%d", len(vectors), len(texts))
        return vectors

    def embed_query(self, text: str, max_wait_s: float = 5) -> list[float]:
        return self._call([text], self.QUERY_TIMEOUT_S)[0]


class OllamaEmbedder(OpenAICompatEmbedder):
    """API native d'Ollama (/api/embed) : seule à accepter le réglage du nombre de threads.

    Mesuré sous Docker Desktop (14 cœurs) : avec les 14 threads par défaut, les threads
    se gênent et une question prend ~3 s ; avec 8 threads, 0,04 s.
    """

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.threads = settings.embed_threads
        self.base = settings.embed_url.rstrip("/").removesuffix("/v1")

    def _call(self, texts: list[str], timeout_s: float) -> list[list[float]]:
        body = {"model": self.model, "input": texts, "truncate": True}
        if self.threads:
            body["options"] = {"num_thread": self.threads}
        try:
            r = self.http.post(self.base + "/api/embed", json=body, timeout=timeout_s)
            r.raise_for_status()
        except Exception as e:
            raise EmbeddingUnavailable(str(e)) from e
        vectors = [_normalize(v) for v in r.json()["embeddings"]]
        if vectors and len(vectors[0]) != self.dim:
            raise ValueError(f"{self.model} donne {len(vectors[0])} dimensions, la base en attend {self.dim}")
        return vectors


class FakeEmbedder:
    """Sac de mots haché : deux textes qui partagent des mots ont des vecteurs proches."""
    model = "fake"

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for word in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        return _normalize(v) if any(v) else [1.0 / math.sqrt(self.dim)] * self.dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str, max_wait_s: float = 5) -> list[float]:
        return self._vec(text)


def make_embedder(settings: Settings) -> Embedder:
    if settings.embed == "gemini":
        return GeminiEmbedder(settings)
    if settings.embed == "ollama":
        return OllamaEmbedder(settings)
    if settings.embed == "openai":
        return OpenAICompatEmbedder(settings)
    if settings.embed == "fake":
        return FakeEmbedder(settings.embed_dim)
    raise ValueError(f"ASSIST_EMBED inconnu : {settings.embed} (ollama, openai, gemini ou fake)")
