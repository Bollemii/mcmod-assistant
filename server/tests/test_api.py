"""Tests de l'API /assist, sans appel réel à Claude.

Lancer (depuis le dossier server/) : python -m unittest discover -s tests -t .
"""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import tokens
from api.llm import SYSTEM_PROMPT, Answer, LLMUnavailable, build_user_message
from api.main import create_app
from api.providers.claude import ClaudeAnswerer
from api.ratelimit import RateLimiter
from api.servers import ServerRegistry
from api.settings import Settings


class FakeAnswerer:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def answer(self, question, server, mc_version, context=()):
        self.calls.append((question, server, mc_version))
        if self.fail:
            raise LLMUnavailable("panne simulée")
        return Answer("Huit pierres taillées autour d'une case vide.", "fake-model", input_tokens=10, output_tokens=5)


class ApiTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.settings = replace(Settings(llm="fake"), tokens_file=tmp / "tokens.txt", log_file=tmp / "queries.jsonl",
                                rate_limit=3, rate_window_s=60)
        self.token = tokens.create(self.settings.tokens_file, "Testeur")
        self.answerer = FakeAnswerer()
        self.client = TestClient(create_app(self.settings, self.answerer))

    def ask(self, token=None, **body):
        body = {"question": "Comment craft un four ?", **body}
        headers = {"Authorization": f"Bearer {token or self.token}"} if token != "" else {}
        return self.client.post("/v1/ask", json=body, headers=headers)

    def test_health(self):
        self.assertEqual(self.client.get("/v1/health").json(), {"status": "ok", "rag": False})

    def test_answer_and_log(self):
        r = self.ask(server="Play.Excalia.fr:25565", mc_version="26.2")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["answer"], "Huit pierres taillées autour d'une case vide.")
        # Sans base ni extrait cité : lien vers le wiki du serveur.
        self.assertEqual(body["sources"], [{"title": "Wiki Excalia", "url": "https://excalia.fr/wiki"}])
        question, server, version = self.answerer.calls[0]
        self.assertEqual((question, server.slug, version), ("Comment craft un four ?", "excalia", "26.2"))
        line = json.loads(self.settings.log_file.read_text("utf8").splitlines()[0])
        self.assertEqual((line["query_id"], line["server"], line["model"]), (body["query_id"], "excalia", "fake-model"))
        self.assertNotIn(self.token, self.settings.log_file.read_text("utf8"))

    def test_unknown_server(self):
        self.ask(server="mc.inconnu.net")
        self.assertIsNone(self.answerer.calls[0][1])

    def test_auth(self):
        self.assertEqual(self.ask(token="").status_code, 401)
        r = self.ask(token="mca_faux")
        self.assertEqual((r.status_code, r.json()), (401, {"error": "Jeton invalide."}))

    def test_token_added_without_restart(self):
        other = tokens.create(self.settings.tokens_file, "Nouveau")
        self.assertEqual(self.ask(token=other).status_code, 200)

    def test_question_validation(self):
        self.assertEqual(self.ask(question="").status_code, 422)
        self.assertEqual(self.ask(question="a" * 300).status_code, 422)
        self.ask(question="  Comment   craft\nun four ?  ")
        self.assertEqual(self.answerer.calls[0][0], "Comment craft un four ?")

    def test_rate_limit(self):
        for _ in range(3):
            self.assertEqual(self.ask().status_code, 200)
        r = self.ask()
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["Retry-After"]), 0)
        other = tokens.create(self.settings.tokens_file, "Autre joueur")
        self.assertEqual(self.ask(token=other).status_code, 200)

    def test_fake_llm_mode(self):
        client = TestClient(create_app(replace(self.settings, llm="fake")))
        r = client.post("/v1/ask", json={"question": "four ?", "server": "play.excalia.fr"},
                        headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.json()["answer"], "(mode test) Question bien reçue pour Excalia, Minecraft ?, 0 extraits : four ?")

    def test_llm_unavailable(self):
        client = TestClient(create_app(self.settings, FakeAnswerer(fail=True)))
        r = client.post("/v1/ask", json={"question": "four ?"}, headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 503)


class FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(stop_reason="end_turn", texts=("Réponse.",)):
    content = [SimpleNamespace(type="thinking", thinking="")] + [SimpleNamespace(type="text", text=t) for t in texts]
    response = SimpleNamespace(stop_reason=stop_reason, content=content, model="claude-opus-5-5",
                               usage=SimpleNamespace(input_tokens=120, output_tokens=40))
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


class ClaudeAnswererTest(unittest.TestCase):
    def run_answer(self, client, server=None):
        return asyncio.run(ClaudeAnswerer(Settings(llm="claude"), client).answer("Comment craft un four ?", server, "26.2"))

    def test_request_parameters(self):
        client, messages = fake_client()
        answer = self.run_answer(client)
        self.assertEqual(answer.text, "Réponse.")
        kw = messages.kwargs
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["output_config"], {"effort": "low"})
        self.assertEqual((kw["fallbacks"], kw["betas"]), ("default", ["server-side-fallback-2026-07-01"]))
        self.assertEqual(kw["system"], SYSTEM_PROMPT)
        self.assertNotIn("thinking", kw)  # Opus 5.5 : toujours actif, réglé par l'effort

    def test_refusal(self):
        client, _ = fake_client(stop_reason="refusal", texts=())
        answer = self.run_answer(client)
        self.assertTrue(answer.refused)

    def test_empty_answer_is_an_error(self):
        client, _ = fake_client(stop_reason="max_tokens", texts=())
        with self.assertRaises(LLMUnavailable):
            self.run_answer(client)

    def test_user_message(self):
        registry = ServerRegistry(Settings().servers_file)
        msg = build_user_message("Comment craft un four ?", registry.find("play.excalia.fr"), None)
        self.assertIn("Serveur : Excalia", msg)
        self.assertIn("Wiki du serveur : https://excalia.fr/wiki", msg)
        self.assertIn("Version de Minecraft : 26.2", msg)
        self.assertIn("inconnu", build_user_message("q", None, None))


class UnitsTest(unittest.TestCase):
    def test_rate_limiter_window(self):
        now = [0.0]
        rl = RateLimiter(2, 60, clock=lambda: now[0])
        self.assertEqual((rl.check("a"), rl.check("a")), (0, 0))
        self.assertAlmostEqual(rl.check("a"), 60)
        now[0] = 61
        self.assertEqual(rl.check("a"), 0)

    def test_server_lookup(self):
        registry = ServerRegistry(Settings().servers_file)
        for address in ("play.excalia.fr", "PLAY.EXCALIA.FR:25565", "excalia.fr", "eu.play.excalia.fr"):
            self.assertEqual(registry.find(address).slug, "excalia", address)
        for address in (None, "", "fr", "excalia.com", "localhost"):
            self.assertIsNone(registry.find(address), address)


if __name__ == "__main__":
    unittest.main()
