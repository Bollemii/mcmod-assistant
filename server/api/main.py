"""API HTTP de l'assistant /assist.

Lancer (depuis le dossier server/) :
    python -m uvicorn api.main:app --host 127.0.0.1 --port 8000

Contrat : docs/architecture.md, section « Contrat de l'API ».

Sans base (ASSIST_DATABASE_URL vide) ou si elle est injoignable, l'API répond quand
même, sans extraits (comportement de la phase 1).
"""
import asyncio
import json
import logging
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import tokens
from .embeddings import Embedder, EmbeddingUnavailable, make_embedder
from .llm import Answer, Answerer, LLMUnavailable, make_answerer
from .ratelimit import RateLimiter
from .db import migrate
from .retrieval import ContextChunk, search
from .servers import ServerInfo, ServerRegistry
from .settings import Settings, from_env

log = logging.getLogger("assist")
MAX_SOURCES = 3
DOCKER_HINT = "Docker est-il lancé ? (docker compose up -d dans server/)"
# Base configurée mais injoignable : on ne laisse pas le LLM répondre de mémoire (il
# inventerait des règles du serveur), on renvoie vers le wiki.
OFFLINE_TEXT = ("Je n'ai pas accès au wiki du serveur pour le moment, je préfère ne pas répondre "
                "de mémoire. Consulte le wiki ou réessaie dans quelques minutes.")
OFFLINE_MODEL = "aucun (base injoignable)"


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
    sources: list[Source] = []
    query_id: str
    cached: bool = False
    degraded: bool = False             # réponse du mode allégé (LLM local), moins complète


class RefreshRequest(BaseModel):
    server: str | None = None
    force: bool = False                # jeton admin : sans délai, reconstruction complète


@dataclass(frozen=True)
class Caller:
    hash: str                          # empreinte du jeton : identifiant du joueur pour la limite
    info: tokens.TokenInfo


def make_sources(answer: Answer, context: list[ContextChunk], server: ServerInfo | None) -> list[Source]:
    """Extraits cités par le LLM -> liens cliquables ; à défaut, le wiki du serveur."""
    out, seen = [], set()
    for n in answer.cited:
        if 1 <= n <= len(context) and context[n - 1].url not in seen:
            c = context[n - 1]
            seen.add(c.url)
            last = c.heading_path[-1] if c.heading_path else ""
            title = f"{c.title} › {last}" if last and last != c.title else c.title
            out.append(Source(title=title[:90], url=c.url))
    if not out and server and server.wiki_url and not answer.refused:
        out.append(Source(title=f"Wiki {server.name}", url=server.wiki_url))
    return out[:MAX_SOURCES]


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def create_app(settings: Settings | None = None, answerer: Answerer | None = None,
               embedder: Embedder | None = None, pool=None, refresher=None) -> FastAPI:
    settings = settings or from_env()

    def setup_rag(pool) -> None:
        """Base, embeddings et mises à jour ; sans base, l'API répond sans extraits."""
        app.state.pool = pool
        app.state.embedder = embedder or (make_embedder(settings) if pool else None)
        if pool is None:
            log.warning("RAG désactivé (ASSIST_DATABASE_URL vide) : réponses sans extraits.")
            app.state.refresher = None
            return
        log.warning("RAG : embeddings %s", app.state.embedder.model)
        if refresher is not None:
            app.state.refresher = refresher
        else:
            from .refresh import RefreshManager
            app.state.refresher = RefreshManager(settings, pool, app.state.embedder,
                                                 tomllib.loads(settings.servers_file.read_text("utf8")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # La base n'est ouverte qu'au démarrage du serveur (pas à l'import du module).
        owned = None
        if app.state.pool is None and settings.database_url:
            from .db import make_pool
            owned = make_pool(settings.database_url)
            try:
                await asyncio.to_thread(owned.wait, 5)
                log.warning("Base : %s", settings.database_url.rsplit("@", 1)[-1])
            except Exception as e:   # la base peut démarrer après l'API : le pool réessaiera
                log.warning("Base injoignable pour l'instant (%s). %s En attendant, /assist renvoie vers le wiki.",
                            e, DOCKER_HINT)
            setup_rag(owned)
            try:   # charge le modèle d'embeddings maintenant plutôt qu'à la première question
                await asyncio.to_thread(app.state.embedder.embed_query, "préchauffage")
            except Exception as e:
                log.warning("Embeddings indisponibles au démarrage (%s) : plein texte seul en attendant. %s",
                            e, DOCKER_HINT)
        if settings.fallback_llm == "ollama" and settings.llm != "fake":
            # charge gemma3:4b en mémoire (~10 s) sans retarder le démarrage
            app.state.warmup = asyncio.create_task(warm_up_ollama())
        loop_task = None
        if app.state.refresher and settings.refresh_check_s > 0:
            loop_task = asyncio.create_task(app.state.refresher.loop())
        yield
        if loop_task:
            loop_task.cancel()
        if owned:
            owned.close()

    async def warm_up_ollama() -> None:
        from .providers.ollama import OllamaAnswerer
        try:
            await OllamaAnswerer(settings).answer("préchauffage", None, None, [])
            log.warning("Mode allégé prêt : %s (Ollama)", settings.ollama_model)
        except Exception as e:
            log.warning("Mode allégé indisponible (%s) : si Gemini échoue, /assist répondra 503. %s", e, DOCKER_HINT)

    app = FastAPI(title="mcmod-assistant", version="0.3.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.answerer = answerer or make_answerer(settings)
    log.warning("LLM : %s, modèle %s", settings.llm, settings.model_name)
    app.state.servers = ServerRegistry(settings.servers_file)
    app.state.limiter = RateLimiter(settings.rate_limit, settings.rate_window_s)
    app.state.pool = app.state.embedder = app.state.refresher = None
    app.state.migrated = False
    if pool is not None or not settings.database_url:
        setup_rag(pool)      # sinon : au démarrage (lifespan)

    def caller(authorization: str | None = Header(default=None)) -> Caller:
        """Vérifie le jeton."""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Jeton manquant.")
        h = tokens.digest(authorization.removeprefix("Bearer ").strip())
        # Relu à chaque requête : un jeton ajouté ou retiré prend effet sans redémarrer.
        info = tokens.load(settings.tokens_file).get(h)
        if info is None:
            raise HTTPException(401, "Jeton invalide.")
        return Caller(h, info)

    def admin(c: Caller = Depends(caller)) -> Caller:
        if not c.info.is_admin:
            raise HTTPException(403, "Commande réservée aux jetons admin.")
        return c

    def check_rate(c: Caller) -> None:
        wait = app.state.limiter.check(c.hash)
        if wait:
            raise HTTPException(429, "Trop de demandes, réessaie dans un instant.",
                                headers={"Retry-After": str(int(wait) + 1)})

    def known_server(address: str | None) -> ServerInfo:
        server = app.state.servers.find(address)
        if not server:
            raise HTTPException(404, "Serveur inconnu de l'assistant.")
        return server

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.get("/v1/health")
    async def health():
        return {"status": "ok", "rag": app.state.pool is not None}

    # --- Questions -------------------------------------------------------------------

    def retrieve(question: str, server: ServerInfo | None) -> tuple[list[ContextChunk], list[float] | None, bool]:
        """Bloquant (thread). Renvoie (extraits, vecteur de la question, base joignable).

        Embeddings indisponibles : plein texte seul. Base injoignable : base joignable = False.
        """
        if not app.state.pool:
            return [], None, True       # pas de base configurée : mode phase 1, assumé
        qvec = None
        try:
            qvec = app.state.embedder.embed_query(question)
        except EmbeddingUnavailable as e:
            log.warning("Embeddings indisponibles, recherche plein texte seule : %s. %s", e, DOCKER_HINT)
        try:
            with app.state.pool.connection(timeout=5) as conn:
                return search(conn, question, qvec, server.slug if server else None,
                              settings.top_k, settings.max_chunks_per_doc), qvec, True
        except Exception as e:
            log.warning("Recherche impossible (%s). %s", e, DOCKER_HINT)
            return [], qvec, False

    def log_query_db(server: ServerInfo | None, c: Caller, question: str, qvec, answer: Answer,
                     context: list[ContextChunk], timings: dict) -> int | None:
        if not app.state.pool:
            return None
        try:
            with app.state.pool.connection(timeout=5) as conn:
                if not app.state.migrated:      # la base a pu démarrer après l'API
                    migrate(conn)
                    app.state.migrated = True
                return conn.execute(
                    """INSERT INTO queries (server_id, player_hash, question, q_embedding, answer, chunk_ids,
                                            chunk_keys, model, latency_ms, search_ms, llm_ms)
                       VALUES ((SELECT id FROM servers WHERE slug = %s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (server.slug if server else None, c.hash[:16], question,
                     np.asarray(qvec, dtype=np.float32) if qvec is not None else None, answer.text,
                     [x.id for x in context], [x.key for x in context], answer.model,
                     timings["latency_ms"], timings["search_ms"], timings["llm_ms"])).fetchone()[0]
        except Exception as e:
            log.warning("Journalisation de la question en base impossible : %s", e)
            return None

    @app.post("/v1/ask", response_model=AskResponse)
    async def ask(req: AskRequest, c: Caller = Depends(caller)):
        question = " ".join(req.question.split())
        if len(question) > settings.max_question_chars:
            raise HTTPException(422, f"Question trop longue ({settings.max_question_chars} caractères au plus).")
        check_rate(c)

        server = app.state.servers.find(req.server)
        started = time.perf_counter()
        context, qvec, reachable = await asyncio.to_thread(retrieve, question, server)
        searched = time.perf_counter()
        llm_ms = None
        if reachable:
            try:
                answer = await app.state.answerer.answer(question, server, req.mc_version, context)
            except LLMUnavailable as e:
                log.warning("LLM indisponible : %s", e)
                raise HTTPException(503, "L'assistant est indisponible pour le moment.")
            llm_ms = int((time.perf_counter() - searched) * 1000)
        else:
            answer = Answer(OFFLINE_TEXT, OFFLINE_MODEL)
        timings = {"latency_ms": int((time.perf_counter() - started) * 1000),
                   "search_ms": int((searched - started) * 1000), "llm_ms": llm_ms}
        sources = make_sources(answer, context, server)

        db_id = await asyncio.to_thread(log_query_db, server, c, question, qvec, answer, context, timings)
        query_id = str(db_id) if db_id else uuid.uuid4().hex[:12]
        record = {"query_id": query_id, "at": datetime.now(timezone.utc).isoformat(),
                  "player": c.hash[:12], "server": server.slug if server else req.server,
                  "mc_version": req.mc_version, "question": question, "answer": answer.text,
                  "refused": answer.refused, "model": answer.model, "degraded": answer.degraded, **timings,
                  "input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens,
                  "chunks": [x.url for x in context], "cited": answer.cited,
                  "sources": [s.url for s in sources]}
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        with settings.log_file.open("a", encoding="utf8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return AskResponse(answer=answer.text, sources=sources, query_id=query_id, degraded=answer.degraded)

    # --- Mise à jour des sources -----------------------------------------------------

    def need_refresher():
        if not app.state.refresher:
            raise HTTPException(503, "Mise à jour indisponible : l'assistant n'a pas de base de données.")
        return app.state.refresher

    @app.post("/v1/refresh")
    async def refresh(req: RefreshRequest, c: Caller = Depends(caller)):
        if req.force and not c.info.is_admin:
            raise HTTPException(403, "La mise à jour forcée est réservée aux jetons admin.")
        check_rate(c)
        server = known_server(req.server)
        refresher = need_refresher()
        log.warning("Mise à jour de %s demandée par %s%s", server.slug, c.info.name, " (forcée)" if req.force else "")
        results = await refresher.refresh(server.slug, force=req.force)
        return {"server": server.slug, "sources": [{k: _iso(v) for k, v in r.items()} for r in results]}

    @app.get("/v1/sources")
    async def sources(server: str | None = None, c: Caller = Depends(admin)):
        info = known_server(server)
        rows = await asyncio.to_thread(need_refresher().status, info.slug)
        return {"server": info.slug, "sources": [{k: _iso(v) for k, v in r.items()} for r in rows]}

    return app


app = create_app()
