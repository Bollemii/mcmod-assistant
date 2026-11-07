"""Mode allégé : bascule Gemini -> Ollama et connecteur Ollama. Doublures seulement, aucun réseau.

Lancer (depuis le dossier server/) : python -m unittest discover -s tests -t .
"""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from api import tokens
from api.fallback import DAILY_QUOTA_PAUSE_S, FallbackAnswerer, pause_after
from api.llm import LIGHT_SYSTEM_PROMPT, SYSTEM_PROMPT, Answer, LLMUnavailable
from api.main import create_app
from api.providers.ollama import OllamaAnswerer
from api.retrieval import ContextChunk
from api.settings import Settings

QUOTA_MINUTE = ("Gemini 429 : 429 RESOURCE_EXHAUSTED. {'error': {'message': 'Please retry in 35.2s.', "
                "'details': [{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}}")
QUOTA_DAY = ("Gemini 429 : 429 RESOURCE_EXHAUSTED. {'error': {'message': 'Please retry in 12.0s.', "
             "'details': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}")


class Scripted:
    """Fournisseur doublure : renvoie une réponse ou lève l'erreur prévue."""

    def __init__(self, name, error=None):
        self.name, self.error, self.calls = name, error, 0

    async def answer(self, question, server, mc_version, context=()):
        self.calls += 1
        if self.error:
            raise LLMUnavailable(self.error)
        return Answer(f"réponse de {self.name}", self.name, cited=[1])


class PauseTest(unittest.TestCase):
    def test_pause_by_error(self):
        self.assertAlmostEqual(pause_after(LLMUnavailable(QUOTA_MINUTE), 60), 36.2)
        self.assertEqual(pause_after(LLMUnavailable(QUOTA_DAY), 60), DAILY_QUOTA_PAUSE_S)
        self.assertEqual(pause_after(LLMUnavailable("Gemini 503 : surcharge"), 60), 60)


class FallbackAnswererTest(unittest.TestCase):
    def run_answer(self, answerer):
        return asyncio.run(answerer.answer("q", None, None, []))

    def test_primary_ok(self):
        primary, light = Scripted("gemini"), Scripted("ollama")
        answer = self.run_answer(FallbackAnswerer(primary, light, Settings()))
        self.assertEqual((answer.model, answer.degraded, light.calls), ("gemini", False, 0))

    def test_switch_then_pause_then_retry(self):
        now = [0.0]
        primary, light = Scripted("gemini", QUOTA_MINUTE), Scripted("ollama")
        answerer = FallbackAnswerer(primary, light, Settings(), clock=lambda: now[0])
        answer = self.run_answer(answerer)
        self.assertEqual((answer.model, answer.degraded), ("ollama", True))
        now[0] = 10                                     # pendant la pause : Gemini n'est pas rappelé
        self.run_answer(answerer)
        self.assertEqual((primary.calls, light.calls), (1, 2))
        now[0] = 40                                     # pause finie : Gemini est réessayé
        primary.error = None
        answer = self.run_answer(answerer)
        self.assertEqual((answer.model, answer.degraded, primary.calls), ("gemini", False, 2))

    def test_both_down(self):
        answerer = FallbackAnswerer(Scripted("gemini", "panne"), Scripted("ollama", "arrêté"), Settings())
        with self.assertRaises(LLMUnavailable):
            self.run_answer(answerer)


class OllamaAnswererTest(unittest.TestCase):
    def test_light_request(self):
        sent = []

        def handler(request):
            sent.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"message": {"content": "Monte au palier 2.\nSOURCES: 1"},
                                             "prompt_eval_count": 900, "eval_count": 20})

        settings = Settings(light_top_k=2, light_extract_chars=100)
        answerer = OllamaAnswerer(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        context = [ContextChunk(i, f"https://excalia.fr/wiki/p{i}", "Page", ["Page", f"S{i}"], "mot " * 200,
                                "server_wiki", None) for i in range(1, 6)]
        answer = asyncio.run(answerer.answer("spawner ?", None, None, context))
        self.assertEqual((answer.text, answer.cited, answer.model), ("Monte au palier 2.", [1], "gemma3:4b"))
        url, body = sent[0]
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(body["messages"][0]["content"], LIGHT_SYSTEM_PROMPT)
        self.assertLess(len(LIGHT_SYSTEM_PROMPT), len(SYSTEM_PROMPT) / 2)
        user = body["messages"][1]["content"]
        self.assertIn("[2]", user)
        self.assertNotIn("[3]", user)                   # 2 extraits seulement…
        self.assertIn("[…]", user)                      # …tronqués
        self.assertEqual((body["think"], body["options"]["num_thread"]), (False, 8))

    def test_unreachable(self):
        def handler(request):
            raise httpx.ConnectError("refusé")
        answerer = OllamaAnswerer(Settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with self.assertRaises(LLMUnavailable):
            asyncio.run(answerer.answer("q", None, None, []))


class ApiDegradedTest(unittest.TestCase):
    def test_degraded_flag_in_response_and_log(self):
        tmp = Path(tempfile.mkdtemp())
        settings = replace(Settings(llm="fake"), tokens_file=tmp / "t.txt", log_file=tmp / "q.jsonl")
        token = tokens.create(settings.tokens_file, "Joueur")
        answerer = FallbackAnswerer(Scripted("gemini", QUOTA_MINUTE), Scripted("ollama"), settings)
        client = TestClient(create_app(settings, answerer))
        r = client.post("/v1/ask", json={"question": "q ?", "server": "play.excalia.fr"},
                        headers={"Authorization": f"Bearer {token}"})
        self.assertEqual((r.status_code, r.json()["degraded"]), (200, True))
        self.assertTrue(json.loads(settings.log_file.read_text("utf8"))["degraded"])


if __name__ == "__main__":
    unittest.main()
