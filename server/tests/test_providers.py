"""Tests des fournisseurs Gemini et compatible OpenAI, et du choix du fournisseur.

Aucun appel réel : les réponses sont construites avec les vrais types du SDK Google,
ou servies par un transport httpx simulé.
"""
import asyncio
import json
import unittest
from types import SimpleNamespace

import httpx
from google.genai import errors, types

from api.llm import REFUSAL_TEXT, SYSTEM_PROMPT, FakeAnswerer, LLMUnavailable, make_answerer
from api.providers.gemini import GeminiAnswerer
from api.providers.openai_compat import OpenAICompatAnswerer
from api.settings import Settings


def gemini_response(text="Huit pierres taillées.", finish=types.FinishReason.STOP, block=None, thought=None):
    parts = ([types.Part(text=thought, thought=True)] if thought else []) + ([types.Part(text=text)] if text else [])
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=block) if block else None,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=120, candidates_token_count=30, thoughts_token_count=15),
        model_version="gemini-3.5-flash-lite",
    )


class FakeGeminiModels:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    async def generate_content(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def gemini(result, **settings):
    models = FakeGeminiModels(result)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    answerer = GeminiAnswerer(Settings(llm="gemini", **settings), client)
    return answerer, models


def ask(answerer):
    return asyncio.run(answerer.answer("Comment craft un four ?", None, "26.2"))


class GeminiTest(unittest.TestCase):
    def test_request_and_answer(self):
        answerer, models = gemini(gemini_response(thought="réflexion interne"))
        answer = ask(answerer)
        self.assertEqual(answer.text, "Huit pierres taillées.")       # la réflexion n'est pas affichée
        self.assertEqual((answer.input_tokens, answer.output_tokens), (120, 45))
        self.assertEqual(models.kwargs["model"], "gemini-3.5-flash-lite")
        config = models.kwargs["config"]
        self.assertEqual(config.system_instruction, SYSTEM_PROMPT)
        self.assertEqual(config.thinking_config.thinking_level, types.ThinkingLevel.LOW)
        self.assertIn("Question du joueur : Comment craft un four ?", models.kwargs["contents"])

    def test_model_and_effort_override(self):
        answerer, models = gemini(gemini_response(), model="gemini-3.8-flash", effort="high")
        ask(answerer)
        self.assertEqual(models.kwargs["model"], "gemini-3.8-flash")
        self.assertEqual(models.kwargs["config"].thinking_config.thinking_level, types.ThinkingLevel.HIGH)

    def test_blocked(self):
        for response in (gemini_response(text=None, finish=types.FinishReason.SAFETY),
                         gemini_response(text=None, block=types.BlockedReason.PROHIBITED_CONTENT)):
            answer = ask(gemini(response)[0])
            self.assertTrue(answer.refused)
            self.assertEqual(answer.text, REFUSAL_TEXT)

    def test_empty_answer(self):
        with self.assertRaises(LLMUnavailable):
            ask(gemini(gemini_response(text=None, finish=types.FinishReason.MAX_TOKENS))[0])

    def test_quota_exceeded(self):
        error = errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
        with self.assertRaises(LLMUnavailable) as ctx:
            ask(gemini(error)[0])
        self.assertIn("429", str(ctx.exception))


def openai(handler, **settings):
    transport = httpx.MockTransport(handler)
    s = Settings(llm="openai", model="mistral-small", openai_base_url="http://llm.local/v1/", **settings)
    return OpenAICompatAnswerer(s, httpx.AsyncClient(transport=transport))


class OpenAICompatTest(unittest.TestCase):
    def test_request_and_answer(self):
        seen = {}

        def handler(request):
            seen["url"], seen["body"] = str(request.url), json.loads(request.content)
            return httpx.Response(200, json={"model": "mistral-small", "usage": {"prompt_tokens": 90, "completion_tokens": 20},
                                             "choices": [{"finish_reason": "stop", "message": {"content": " Réponse. "}}]})

        answer = ask(openai(handler))
        self.assertEqual(seen["url"], "http://llm.local/v1/chat/completions")
        self.assertEqual([m["role"] for m in seen["body"]["messages"]], ["system", "user"])
        self.assertEqual((answer.text, answer.input_tokens, answer.output_tokens), ("Réponse.", 90, 20))

    def test_errors(self):
        with self.assertRaises(LLMUnavailable):
            ask(openai(lambda r: httpx.Response(500, text="panne")))
        refused = ask(openai(lambda r: httpx.Response(200, json={"choices": [{"finish_reason": "content_filter", "message": {}}]})))
        self.assertTrue(refused.refused)

    def test_base_url_required(self):
        with self.assertRaises(ValueError):
            OpenAICompatAnswerer(Settings(llm="openai", model="x"))


class FactoryTest(unittest.TestCase):
    def test_default_is_gemini(self):
        self.assertEqual(Settings().llm, "gemini")
        self.assertIsInstance(make_answerer(Settings()), GeminiAnswerer)   # sans clé : créé quand même

    def test_choices(self):
        self.assertIsInstance(make_answerer(Settings(llm="fake")), FakeAnswerer)
        self.assertEqual(type(make_answerer(Settings(llm="claude"))).__name__, "ClaudeAnswerer")
        self.assertEqual(Settings(llm="claude").model_name, "claude-opus-5-5")
        with self.assertRaises(ValueError):
            make_answerer(Settings(llm="inconnu"))
        with self.assertRaises(ValueError):
            Settings(llm="openai").model_name    # modèle obligatoire


if __name__ == "__main__":
    unittest.main()
