"""Lecture de la configuration depuis server/.env."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api.settings import SERVER_DIR, from_env


def env_file(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / ".env"
    path.write_text(text, "utf8")
    return path


class EnvFileTest(unittest.TestCase):
    def test_values_from_file(self):
        path = env_file("ASSIST_LLM=claude\nANTHROPIC_API_KEY=sk-test\nASSIST_MODEL=claude-sonnet-5-5\n"
                        "ASSIST_RATE_LIMIT=5\nASSIST_TOKENS_FILE=config/autres.txt\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            s = from_env(path)
            self.assertEqual((s.llm, s.model_name, s.rate_limit), ("claude", "claude-sonnet-5-5", 5))
            self.assertEqual(s.tokens_file, SERVER_DIR / "config" / "autres.txt")   # relatif à server/
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-test")           # lu par le SDK

    def test_environment_wins_over_file(self):
        path = env_file("ASSIST_LLM=claude\nGEMINI_API_KEY=cle-du-fichier\n")
        with mock.patch.dict(os.environ, {"ASSIST_LLM": "fake", "GEMINI_API_KEY": "cle-env"}, clear=True):
            self.assertEqual(from_env(path).llm, "fake")
            self.assertEqual(os.environ["GEMINI_API_KEY"], "cle-env")

    def test_missing_file_and_empty_values(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = from_env(Path(tempfile.mkdtemp()) / "absent.env")
            self.assertEqual((s.llm, s.model_name), ("gemini", "gemini-3.5-flash-lite"))
        with mock.patch.dict(os.environ, {}, clear=True):
            s = from_env(env_file("ASSIST_MODEL=\nGEMINI_API_KEY=\n"))      # lignes laissées vides
            self.assertEqual(s.model_name, "gemini-3.5-flash-lite")


if __name__ == "__main__":
    unittest.main()
