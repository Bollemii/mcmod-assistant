"""Tests d'intégration avec PostgreSQL + pgvector : chargement, recherche, mise à jour, /v1/ask.

Ignorés sans base de test. Avec la base de docker-compose.yml :
    ASSIST_TEST_DATABASE_URL=postgresql://assist:assist@127.0.0.1:5433/assist_test \
        python -m unittest tests.test_rag_db
La base assist_test est recréée à chaque lancement (jamais la base principale).
"""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from api import tokens
from api.db import connect, make_pool
from api.embeddings import FakeEmbedder
from api.llm import Answer
from api.main import create_app
from api.refresh import RefreshManager
from api.retrieval import search
from api.settings import SERVER_DIR, Settings
from db.load import load_source, sync_config

TEST_URL = os.environ.get("ASSIST_TEST_DATABASE_URL")
WIKI = "https://excalia.fr/wiki/"

SERVERS = {
    "excalia": {"name": "Excalia", "addresses": ["play.excalia.fr"], "season": "saison3",
                "wiki_url": "https://excalia.fr/wiki",
                "sources": {"wiki": {"kind": "server_wiki", "ingester": "excalia_wiki", "priority": 100,
                                     "refresh_days": 7}},
                "chat": {"roles": {"joueur": 0.3}}},
    "autre": {"name": "Autre", "addresses": ["autre.net"], "season": None,
              "sources": {"wiki": {"kind": "server_wiki", "priority": 100, "refresh_days": 0}}},
}

PAGES = {
    "excalia": {
        "ile": ("L'île", [("Spawners", "Le palier 2 débloque ton premier spawner. Monte de palier avec /is upgrade."),
                          ("Membres", "Une île accueille 7 joueurs : le chef et 6 membres.")]),
        "peche": ("La pêche", [("Cannes", "La canne de plasma coûte 20 000 dollars au marchand.")]),
        "nether": ("Dimensions", [("Nether", "Il n'y a ni Nether ni End sur le serveur.")]),
    },
    "autre": {"spawn": ("Spawners ailleurs", [("Spawners", "Sur Autre, les spawners se posent dès le palier 1.")])},
}


class CountingEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__(1024)
        self.documents = 0

    def embed_documents(self, texts):
        self.documents += len(texts)
        return super().embed_documents(texts)


def write_source(data_dir: Path, server: str, pages: dict) -> None:
    folder = data_dir / server / "wiki"
    folder.mkdir(parents=True, exist_ok=True)
    state, chunks = {}, []
    for slug, (title, sections) in pages.items():
        url = WIKI + slug
        state[url] = {"hash": str(hash(json.dumps(sections))), "title": title}
        for i, (heading, text) in enumerate(sections):
            chunks.append({"url": f"{url}#s{i}", "title": title, "heading_path": [title, heading], "content": text,
                           "id": f"{slug}{i}", "embed_text": f"{title} > {heading}\n{text}",
                           "category_name": "Test"})
    (folder / "state.json").write_text(json.dumps(state), "utf8")
    with (folder / "chunks.jsonl").open("w", encoding="utf8") as f:
        f.writelines(json.dumps(c) + "\n" for c in chunks)


@unittest.skipUnless(TEST_URL, "ASSIST_TEST_DATABASE_URL non défini")
class RagDatabaseTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        params = conninfo_to_dict(TEST_URL)
        dbname = params["dbname"]
        if dbname == "assist":
            self.fail("ASSIST_TEST_DATABASE_URL doit désigner une base de test, pas la base principale")
        with psycopg.connect(make_conninfo(**{**params, "dbname": "postgres"}), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{dbname}"')
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute((SERVER_DIR / "db" / "schema.sql").read_text("utf8"))
        self.conn = connect(TEST_URL)
        sync_config(self.conn, SERVERS)
        self.data = Path(tempfile.mkdtemp())
        for server, pages in PAGES.items():
            write_source(self.data, server, pages)
        self.embedder = CountingEmbedder()

    def tearDown(self):
        self.conn.close()

    def load(self, server="excalia", force=False):
        return load_source(self.conn, self.embedder, server, "wiki", SERVERS[server]["season"], force=force,
                           data_dir=self.data)

    def ask_search(self, question, server="excalia"):
        return search(self.conn, question, self.embedder.embed_query(question), server, top_k=8)

    def test_incremental_load(self):
        stats = self.load()
        self.assertEqual((stats["added"], stats["changed"], stats["removed"], stats["embedded"]), (3, 0, 0, 4))
        self.assertEqual(self.load(), {"added": 0, "changed": 0, "removed": 0, "unchanged": 3, "embedded": 0})

        pages = dict(PAGES["excalia"])
        title, sections = pages["ile"]
        pages["ile"] = (title, [sections[0], ("Membres", "Une île accueille 8 joueurs.")])   # 1 section changée
        del pages["nether"]
        write_source(self.data, "excalia", pages)
        stats = self.load()
        self.assertEqual((stats["changed"], stats["removed"], stats["unchanged"], stats["embedded"]), (1, 1, 1, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0], 3)
        self.assertIsNotNone(self.conn.execute("SELECT refreshed_at FROM sources WHERE config_key = 'wiki' "
                                               "AND server_id = (SELECT id FROM servers WHERE slug = 'excalia')")
                             .fetchone()[0])

        # force : tout est réécrit, mais rien n'est compté comme modifié ni réembeddé
        before = self.conn.execute("SELECT max(fetched_at) FROM documents").fetchone()[0]
        stats = self.load(force=True)
        self.assertEqual((stats["changed"], stats["unchanged"], stats["embedded"]), (0, 2, 0))
        self.assertGreater(self.conn.execute("SELECT min(fetched_at) FROM documents").fetchone()[0], before)

    def test_search_hybrid_and_server_filter(self):
        self.load()
        self.load("autre")
        results = self.ask_search("comment poser mon premier spawner ?")
        self.assertEqual(results[0].url, WIKI + "ile#s0")
        self.assertTrue(all(r.url.startswith(WIKI) and "spawn" not in r.url.split("/")[-1] for r in results))
        self.assertEqual(self.ask_search("spawners palier", server="autre")[0].url, WIKI + "spawn#s0")
        # plein texte seul (embeddings indisponibles) : « plasma » est retrouvé
        results = search(self.conn, "prix canne plasma", None, "excalia")
        self.assertEqual(results[0].url, WIKI + "peche#s0")
        self.assertEqual(results[0].ranks["vec"], None)
        # serveur inconnu : seulement les sources communes (aucune ici)
        self.assertEqual(self.ask_search("spawner", server=None), [])

    def test_max_chunks_per_doc(self):
        self.load()
        results = search(self.conn, "île", self.embedder.embed_query("île"), "excalia", top_k=8, max_per_doc=1)
        urls = [r.url.split("#")[0] for r in results]
        self.assertEqual(len(urls), len(set(urls)))

    def test_refresh_manager(self):
        pool = make_pool(TEST_URL)
        self.addCleanup(pool.close)
        ingested = []
        settings = replace(Settings(), refresh_cooldown_min=60)
        import api.refresh as refresh_module
        original = refresh_module.load_source
        refresh_module.load_source = lambda conn, emb, slug, key, season, force=False: load_source(
            conn, emb, slug, key, season, force=force, data_dir=self.data)
        self.addCleanup(setattr, refresh_module, "load_source", original)
        manager = RefreshManager(settings, pool, self.embedder, SERVERS, ingest=lambda s, f: ingested.append((s.key, f)))

        self.assertEqual([(s.server, s.key) for s in manager.due()], [("excalia", "wiki")])   # jamais chargée
        first = asyncio.run(manager.refresh("excalia"))
        self.assertEqual((first[0]["status"], first[0]["added"]), ("done", 3))
        self.assertEqual(manager.due(), [])
        self.assertEqual([(s.server, s.key) for s in manager.due(datetime.now(timezone.utc) + timedelta(days=8))],
                         [("excalia", "wiki")])

        second = asyncio.run(manager.refresh("excalia"))
        self.assertEqual(second[0]["status"], "recent")                 # délai minimal
        self.assertGreater(second[0]["retry_after_s"], 3000)
        forced = asyncio.run(manager.refresh("excalia", force=True))
        self.assertEqual(forced[0]["status"], "done")
        self.assertEqual(ingested, [("wiki", False), ("wiki", True)])

        status = manager.status("excalia")[0]
        self.assertEqual((status["documents"], status["chunks"], status["refresh_days"]), (3, 4, 7))

    def test_ask_end_to_end(self):
        self.load()
        pool = make_pool(TEST_URL)
        self.addCleanup(pool.close)

        class CitingAnswerer:
            def __init__(self):
                self.context = None

            async def answer(self, question, server, mc_version, context=()):
                self.context = list(context)
                return Answer("Au palier 2, avec /is upgrade.", "fake", cited=[1])

        tmp = Path(tempfile.mkdtemp())
        settings = replace(Settings(llm="fake"), tokens_file=tmp / "t.txt", log_file=tmp / "q.jsonl",
                           refresh_check_s=0)
        token = tokens.create(settings.tokens_file, "Testeur")
        answerer = CitingAnswerer()
        client = TestClient(create_app(settings, answerer, embedder=self.embedder, pool=pool))
        r = client.post("/v1/ask", json={"question": "comment poser mon premier spawner", "server": "play.excalia.fr"},
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["sources"], [{"title": "L'île › Spawners", "url": WIKI + "ile#s0"}])
        self.assertEqual(answerer.context[0].url, WIKI + "ile#s0")
        row = self.conn.execute("SELECT question, answer, array_length(chunk_ids, 1), q_embedding IS NOT NULL "
                                "FROM queries WHERE id = %s", (int(body["query_id"]),)).fetchone()
        self.assertEqual(row, ("comment poser mon premier spawner", "Au palier 2, avec /is upgrade.",
                               len(answerer.context), True))
        keys, search_ms, llm_ms, latency = self.conn.execute(
            "SELECT chunk_keys, search_ms, llm_ms, latency_ms FROM queries WHERE id = %s",
            (int(body["query_id"]),)).fetchone()
        self.assertEqual(keys[0], "excalia:wiki:ile0")          # clé stable, survit à un rechargement
        self.assertTrue(0 <= search_ms <= latency and 0 <= llm_ms <= latency)

    def test_migration_on_old_database(self):
        self.conn.execute("ALTER TABLE queries DROP COLUMN chunk_keys, DROP COLUMN search_ms, DROP COLUMN llm_ms")
        self.conn.commit()
        from api.db import migrate
        migrate(self.conn)
        migrate(self.conn)                                       # idempotente
        cols = {r[0] for r in self.conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'queries'")}
        self.assertTrue({"chunk_keys", "search_ms", "llm_ms"} <= cols)


if __name__ == "__main__":
    unittest.main()
