"""Mise à jour des sources : automatique (rythme hebdomadaire) et à la demande (/assist maj).

Une mise à jour est légère : le script d'ingestion de la source (servers.toml,
clé `ingester`) retélécharge le contenu, puis db/load.py ne recharge que les pages
nouvelles, modifiées ou supprimées, et ne recalcule que les embeddings des textes
qui ont changé.

- Automatique : toutes les ASSIST_REFRESH_CHECK_S secondes (et au démarrage), chaque
  source dont la dernière mise à jour date de plus de `refresh_days` jours est relancée.
- À la demande : POST /v1/refresh. Une source mise à jour il y a moins de
  ASSIST_REFRESH_COOLDOWN_MIN minutes n'est pas relancée, sauf avec force (jeton admin).
  Une source déjà en cours de mise à jour n'est jamais lancée deux fois.
"""
import asyncio
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from psycopg_pool import ConnectionPool

from db.load import load_source
from .embeddings import Embedder
from .settings import SERVER_DIR, Settings

log = logging.getLogger("assist")
INGESTION_DIR = SERVER_DIR / "ingestion"
INGEST_TIMEOUT_S = 900


@dataclass(frozen=True)
class SourceConf:
    server: str
    key: str
    kind: str
    ingester: str | None
    refresh_days: float
    season: str | None


def sources_from_config(servers: dict, default_days: float) -> list[SourceConf]:
    return [SourceConf(slug, key, src["kind"], src.get("ingester"), float(src.get("refresh_days", default_days)),
                       conf.get("season"))
            for slug, conf in servers.items() for key, src in conf.get("sources", {}).items()]


def run_ingester(source: SourceConf, force: bool) -> None:
    """Lance le script d'ingestion dans un processus à part (il écrit data/servers/…)."""
    cmd = [sys.executable, str(INGESTION_DIR / f"{source.ingester}.py"), "--server", source.server]
    if force:
        cmd.append("--refetch")
    result = subprocess.run(cmd, cwd=INGESTION_DIR, capture_output=True, text=True, encoding="utf8",
                            errors="replace", timeout=INGEST_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(f"ingestion {source.server}/{source.key} en échec : "
                           f"{(result.stderr or result.stdout).strip()[-500:]}")
    log.info("Ingestion %s/%s : %s", source.server, source.key, result.stdout.strip().replace("\n", " | "))


class RefreshManager:
    def __init__(self, settings: Settings, pool: ConnectionPool, embedder: Embedder, servers: dict,
                 ingest: Callable[[SourceConf, bool], None] = run_ingester):
        self.settings = settings
        self.pool = pool
        self.embedder = embedder
        self.sources = sources_from_config(servers, settings.refresh_days)
        self.ingest = ingest
        self.running: dict[tuple[str, str], asyncio.Task] = {}

    # --- état ------------------------------------------------------------------------

    def refreshed_at(self) -> dict[tuple[str, str], datetime | None]:
        with self.pool.connection() as conn:
            rows = conn.execute("""SELECT se.slug, so.config_key, so.refreshed_at
                                   FROM sources so JOIN servers se ON se.id = so.server_id""").fetchall()
        return {(slug, key): at for slug, key, at in rows}

    def status(self, server: str) -> list[dict]:
        with self.pool.connection() as conn:
            counts = {(slug, key): (docs, chunks) for slug, key, docs, chunks in conn.execute(
                """SELECT se.slug, so.config_key, count(DISTINCT d.id), count(c.id)
                   FROM sources so JOIN servers se ON se.id = so.server_id
                   LEFT JOIN documents d ON d.source_id = so.id LEFT JOIN chunks c ON c.document_id = d.id
                   GROUP BY se.slug, so.config_key""")}
        dates = self.refreshed_at()
        out = []
        for s in self.sources:
            if s.server != server:
                continue
            at = dates.get((s.server, s.key))
            nxt = at + timedelta(days=s.refresh_days) if at and s.refresh_days > 0 else None
            docs, chunks = counts.get((s.server, s.key), (0, 0))
            out.append({"source": s.key, "kind": s.kind, "refreshed_at": at, "refresh_days": s.refresh_days,
                        "next_refresh": nxt, "documents": docs, "chunks": chunks,
                        "running": (s.server, s.key) in self.running})
        return out

    def due(self, now: datetime | None = None) -> list[SourceConf]:
        """Sources à mettre à jour automatiquement."""
        now = now or datetime.now(timezone.utc)
        dates = self.refreshed_at()
        return [s for s in self.sources
                if s.refresh_days > 0 and s.ingester
                and (dates.get((s.server, s.key)) is None
                     or now - dates[(s.server, s.key)] >= timedelta(days=s.refresh_days))]

    # --- exécution -------------------------------------------------------------------

    def _refresh_one(self, source: SourceConf, force: bool) -> dict:
        """Bloquant : ingestion puis chargement. Lancé dans un thread."""
        if source.ingester:
            self.ingest(source, force)
        with self.pool.connection() as conn:
            stats = load_source(conn, self.embedder, source.server, source.key, source.season, force=force)
        log.warning("Mise à jour %s/%s : %s", source.server, source.key, stats)
        return stats

    def _start(self, source: SourceConf, force: bool) -> asyncio.Task:
        key = (source.server, source.key)
        task = asyncio.create_task(asyncio.to_thread(self._refresh_one, source, force))
        self.running[key] = task
        task.add_done_callback(lambda t: self.running.pop(key, None))
        return task

    async def refresh(self, server: str, force: bool = False, wait_s: float = 60) -> list[dict]:
        """Met à jour les sources d'un serveur ; attend au plus wait_s secondes le résultat."""
        cooldown = timedelta(minutes=self.settings.refresh_cooldown_min)
        now = datetime.now(timezone.utc)
        dates = await asyncio.to_thread(self.refreshed_at)
        results, tasks = {}, {}
        for s in self.sources:
            if s.server != server:
                continue
            at = dates.get((s.server, s.key))
            if (s.server, s.key) in self.running:
                results[s.key] = {"source": s.key, "status": "running", "refreshed_at": at}
            elif not force and at and now - at < cooldown:
                results[s.key] = {"source": s.key, "status": "recent", "refreshed_at": at,
                                  "retry_after_s": int((at + cooldown - now).total_seconds()) + 1}
            else:
                tasks[s.key] = self._start(s, force)
        if tasks:
            await asyncio.wait([asyncio.shield(t) for t in tasks.values()], timeout=wait_s)
        for key, task in tasks.items():
            if not task.done():
                results[key] = {"source": key, "status": "running", "refreshed_at": dates.get((server, key))}
            elif task.exception():
                log.warning("Mise à jour %s/%s en échec : %s", server, key, task.exception())
                results[key] = {"source": key, "status": "error", "refreshed_at": dates.get((server, key))}
            else:
                results[key] = {"source": key, "status": "done", "refreshed_at": now, **task.result()}
        return [results[s.key] for s in self.sources if s.server == server]

    async def loop(self) -> None:
        """Mise à jour automatique : vérifie au démarrage, puis toutes les refresh_check_s secondes."""
        while True:
            try:
                for source in await asyncio.to_thread(self.due):
                    if (source.server, source.key) not in self.running:
                        log.warning("Mise à jour automatique de %s/%s", source.server, source.key)
                        try:
                            await self._start(source, force=False)
                        except Exception as e:
                            log.warning("Mise à jour automatique de %s/%s en échec : %s",
                                        source.server, source.key, e)
            except Exception as e:   # base indisponible : on réessaiera au prochain tour
                log.warning("Vérification des mises à jour impossible : %s", e)
            await asyncio.sleep(self.settings.refresh_check_s)
