"""API HTTP de l'assistant /assist.

Lancer (depuis le dossier server/) :
    python -m uvicorn api.main:app --host 127.0.0.1 --port 8000

Contrat : docs/architecture.md, section « Contrat de l'API ».
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import tokens
from .llm import Answerer, LLMUnavailable, make_answerer
from .ratelimit import RateLimiter
from .servers import ServerRegistry
from .settings import Settings, from_env

log = logging.getLogger("assist")


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    server: str | None = None          # adresse du serveur, ex. play.excalia.fr
    mc_version: str | None = None
    lang: str = "fr"


class Source(BaseModel):
    title: str
    url: str


class AskResponse(BaseModel):
    answer: str
    sources: list[Source] = []        # rempli à partir de la phase 2 (RAG)
    query_id: str
    cached: bool = False


def create_app(settings: Settings | None = None, answerer: Answerer | None = None) -> FastAPI:
    settings = settings or from_env()
    app = FastAPI(title="mcmod-assistant", version="0.1.0")
    app.state.settings = settings
    app.state.answerer = answerer or make_answerer(settings)
    log.warning("LLM : %s, modèle %s", settings.llm, settings.model_name)
    app.state.servers = ServerRegistry(settings.servers_file)
    app.state.limiter = RateLimiter(settings.rate_limit, settings.rate_window_s)

    def player(authorization: str | None = Header(default=None)) -> str:
        """Vérifie le jeton ; renvoie son empreinte (identifiant du joueur pour la limite)."""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Jeton manquant.")
        h = tokens.digest(authorization.removeprefix("Bearer ").strip())
        # Relu à chaque requête : un jeton ajouté ou retiré prend effet sans redémarrer.
        if h not in tokens.load(settings.tokens_file):
            raise HTTPException(401, "Jeton invalide.")
        return h

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.get("/v1/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/ask", response_model=AskResponse)
    async def ask(req: AskRequest, player_hash: str = Depends(player)):
        question = " ".join(req.question.split())
        if len(question) > settings.max_question_chars:
            raise HTTPException(422, f"Question trop longue ({settings.max_question_chars} caractères au plus).")
        wait = app.state.limiter.check(player_hash)
        if wait:
            raise HTTPException(429, "Trop de questions, réessaie dans un instant.",
                                headers={"Retry-After": str(int(wait) + 1)})

        server = app.state.servers.find(req.server)
        started = time.perf_counter()
        try:
            answer = await app.state.answerer.answer(question, server, req.mc_version)
        except LLMUnavailable as e:
            log.warning("LLM indisponible : %s", e)
            raise HTTPException(503, "L'assistant est indisponible pour le moment.")
        latency_ms = int((time.perf_counter() - started) * 1000)

        query_id = uuid.uuid4().hex[:12]
        record = {"query_id": query_id, "at": datetime.now(timezone.utc).isoformat(),
                  "player": player_hash[:12], "server": server.slug if server else req.server,
                  "mc_version": req.mc_version, "question": question, "answer": answer.text,
                  "refused": answer.refused, "model": answer.model, "latency_ms": latency_ms,
                  "input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens}
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        with settings.log_file.open("a", encoding="utf8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return AskResponse(answer=answer.text, query_id=query_id)

    return app


app = create_app()
