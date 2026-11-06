"""Tests de la phase 2 qui ne demandent pas de base : prompt, sources, jetons, droits.

Lancer (depuis le dossier server/) : python -m unittest discover -s tests -t .
"""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from api import tokens
from api.embeddings import FakeEmbedder
from api.llm import Answer, build_user_message, split_sources
from api.main import create_app, make_sources
from api.retrieval import ContextChunk
from api.servers import ServerRegistry
from api.settings import Settings


def chunk(n, url, title="Page", path=("Page", "Section"), kind="server_wiki", published=None, content="Texte."):
    return ContextChunk(n, url, title, list(path), content, kind, published)


class SplitSourcesTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(split_sources("Pose-le au palier 2.\nSOURCES: 1, 3"), ("Pose-le au palier 2.", [1, 3]))
        self.assertEqual(split_sources("Je ne sais pas.\nSOURCES: aucune"), ("Je ne sais pas.", []))
        self.assertEqual(split_sources("Réponse sans ligne finale."), ("Réponse sans ligne finale.", []))
        self.assertEqual(split_sources("Réponse. **SOURCES:** 2, 2"), ("Réponse.", [2]))
        # un « source : » dans la phrase n'est pas la ligne finale
        self.assertEqual(split_sources("Selon la source : le wiki.\nSOURCES: 4"), ("Selon la source : le wiki.", [4]))


class PromptTest(unittest.TestCase):
    def test_extracts_in_user_message(self):
        server = ServerRegistry(Settings().servers_file).find("play.excalia.fr")
        context = [chunk(1, "https://excalia.fr/wiki/a#s", path=("Île", "Spawners")),
                   chunk(2, "https://excalia.fr/patch-notes/3-6", kind="server_news", path=("Patch 3.6", "Oxygène"),
                         published=datetime(2026, 11, 1, tzinfo=timezone.utc), content="mot " * 1000)]
        msg = build_user_message("Combien de spawners ?", server, None, context)
        self.assertIn("[1] Wiki — Île > Spawners", msg)
        self.assertIn("[2] Note de patch du 01/11/2026 — Patch 3.6 > Oxygène", msg)
        self.assertIn("[…]", msg)                       # extrait trop long tronqué
        self.assertLess(len(msg), 4000)
        self.assertTrue(msg.endswith("Question du joueur : Combien de spawners ?"))
        self.assertIn("aucun extrait", build_user_message("q", server, None, []))


class MakeSourcesTest(unittest.TestCase):
    def setUp(self):
        self.server = ServerRegistry(Settings().servers_file).find("play.excalia.fr")
        self.context = [chunk(1, "https://excalia.fr/wiki/ile#a", title="L'île", path=("L'île", "Paliers")),
                        chunk(2, "https://excalia.fr/wiki/ile#a", title="L'île", path=("L'île", "Paliers")),
                        chunk(3, "https://excalia.fr/patch-notes/3-6", title="3.6", path=("3.6",))]

    def test_cited_sources(self):
        sources = make_sources(Answer("r", "m", cited=[2, 1, 3, 9]), self.context, self.server)
        self.assertEqual([(s.title, s.url) for s in sources],
                         [("L'île › Paliers", "https://excalia.fr/wiki/ile#a"),
                          ("3.6", "https://excalia.fr/patch-notes/3-6")])

    def test_fallback_to_wiki(self):
        self.assertEqual([s.url for s in make_sources(Answer("r", "m"), self.context, self.server)],
                         ["https://excalia.fr/wiki"])
        self.assertEqual(make_sources(Answer("r", "m", refused=True), self.context, self.server), [])
        self.assertEqual(make_sources(Answer("r", "m"), [], None), [])


class GeminiEmbedderTest(unittest.TestCase):
    """Régulation du quota gratuit, sans appel réseau."""

    def make(self, fail_over_tokens=None):
        from types import SimpleNamespace
        from google.genai import errors
        from api.embeddings import GeminiEmbedder, MinuteBudget, estimate_tokens
        now, calls = [0.0], []

        def embed_content(model, contents, config):
            texts = [c.parts[0].text for c in contents]
            calls.append((now[0], len(texts)))
            if fail_over_tokens and sum(estimate_tokens(t) for t in texts) > fail_over_tokens:
                raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message":
                    "Quota exceeded, quotaId: EmbedContentTokensPerMinutePerProjectPerModel-FreeTier"}})
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0]) for _ in texts])

        def sleep(s):
            now[0] += s
        client = SimpleNamespace(models=SimpleNamespace(embed_content=embed_content))
        budget = MinuteBudget(GeminiEmbedder.TEXTS_PER_MIN, GeminiEmbedder.TOKENS_PER_MIN, clock=lambda: now[0], sleep=sleep)
        return GeminiEmbedder(Settings(), client=client, budget=budget), calls

    def test_batches_respect_quota_per_minute(self):
        emb, calls = self.make()
        vectors = emb.embed_documents(["mot " * 500] * 150)        # ~800 tokens estimés chacun
        self.assertEqual(len(vectors), 150)
        self.assertAlmostEqual(vectors[0][0], 0.6)                  # vecteurs normalisés
        self.assertTrue(all(n * 801 <= emb.TOKENS_PER_MIN for _, n in calls))
        # jamais plus de 27 000 tokens estimés sur une fenêtre de 60 s
        for t, _ in calls:
            self.assertLessEqual(sum(n for t2, n in calls if t - 60 < t2 <= t) * 801, emb.TOKENS_PER_MIN)

    def test_oversized_batch_is_split(self):
        emb, calls = self.make(fail_over_tokens=10_000)
        vectors = emb.embed_documents(["mot " * 500] * 30)
        self.assertEqual(len(vectors), 30)
        self.assertGreater(len(calls), 2)

    def test_small_caps_cost_more(self):
        from api.embeddings import estimate_tokens
        self.assertGreater(estimate_tokens("ᴄᴀᴏᴜᴛᴄʜᴏᴜᴄ" * 10), estimate_tokens("caoutchouc" * 10) * 2)


class OllamaEmbedderTest(unittest.TestCase):
    """Requêtes envoyées à Ollama, avec un faux transport HTTP (aucun réseau)."""

    def test_native_api_with_threads(self):
        import httpx
        from api.embeddings import EmbeddingUnavailable, OllamaEmbedder
        sent = []

        def handler(request):
            sent.append((str(request.url), json.loads(request.content)))
            n = len(json.loads(request.content)["input"])
            return httpx.Response(200, json={"embeddings": [[3.0, 4.0] + [0.0] * 1022] * n})

        emb = OllamaEmbedder(Settings(embed="ollama", embed_threads=8))
        emb.http = httpx.Client(transport=httpx.MockTransport(handler))
        vectors = emb.embed_documents([f"texte {i}" for i in range(10)])
        self.assertEqual(len(vectors), 10)
        self.assertAlmostEqual(vectors[0][0], 0.6)                   # normalisé
        url, body = sent[0]
        self.assertEqual(url, "http://127.0.0.1:11434/api/embed")
        self.assertEqual((body["model"], body["options"], len(body["input"])), ("bge-m3", {"num_thread": 8}, 8))
        emb.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        with self.assertRaises(EmbeddingUnavailable):
            emb.embed_query("question")


class TokensTest(unittest.TestCase):
    def test_roles_and_old_format(self):
        path = Path(tempfile.mkdtemp()) / "tokens.txt"
        path.write_text("abc123  Ancien Joueur\n", "utf8")      # format de la phase 1
        admin = tokens.create(path, "Chef", tokens.ADMIN)
        player = tokens.create(path, "admin du dimanche")         # nom qui commence par un type
        loaded = tokens.load(path)
        self.assertEqual(loaded["abc123"], tokens.TokenInfo("Ancien Joueur", tokens.PLAYER))
        self.assertEqual(loaded[tokens.digest(admin)], tokens.TokenInfo("Chef", tokens.ADMIN))
        self.assertEqual(loaded[tokens.digest(player)], tokens.TokenInfo("admin du dimanche", tokens.PLAYER))
        with self.assertRaises(ValueError):
            tokens.create(path, "x", "modo")


class RightsWithoutDatabaseTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.settings = replace(Settings(llm="fake"), tokens_file=tmp / "tokens.txt", log_file=tmp / "q.jsonl")
        self.player = tokens.create(self.settings.tokens_file, "Joueur")
        self.admin = tokens.create(self.settings.tokens_file, "Admin", tokens.ADMIN)
        self.client = TestClient(create_app(self.settings, embedder=FakeEmbedder()))

    def post(self, token, **body):
        return self.client.post("/v1/refresh", json={"server": "play.excalia.fr", **body},
                                headers={"Authorization": f"Bearer {token}"})

    def test_force_needs_admin(self):
        self.assertEqual(self.post(self.player, force=True).status_code, 403)

    def test_no_database(self):
        r = self.post(self.player)
        self.assertEqual(r.status_code, 503)
        self.assertIn("base", r.json()["error"])
        self.assertEqual(self.post(self.admin, force=True).status_code, 503)

    def test_unknown_server(self):
        r = self.client.post("/v1/refresh", json={"server": "mc.inconnu.net"},
                             headers={"Authorization": f"Bearer {self.player}"})
        self.assertEqual(r.status_code, 404)

    def test_sources_needs_admin(self):
        headers = lambda t: {"Authorization": f"Bearer {t}"}
        url = "/v1/sources?server=play.excalia.fr"
        self.assertEqual(self.client.get(url, headers=headers(self.player)).status_code, 403)
        self.assertEqual(self.client.get(url, headers=headers(self.admin)).status_code, 503)
        self.assertEqual(self.client.get(url).status_code, 401)


if __name__ == "__main__":
    unittest.main()
