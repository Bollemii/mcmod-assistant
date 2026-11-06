"""Configuration de l'API, lue dans les variables d'environnement.

Les clés et réglages peuvent être mis dans server/.env (modèle : server/.env.example).
Une variable déjà définie dans l'environnement l'emporte sur le fichier.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = SERVER_DIR / ".env"

# Modèle utilisé par défaut pour chaque fournisseur (ASSIST_MODEL le remplace).
DEFAULT_MODELS = {
    "gemini": "gemini-3.5-flash-lite",   # gratuit (palier gratuit de l'API Gemini), rapide
    "claude": "claude-opus-5-5",         # API Claude : facturée à l'usage, hors abonnement Pro
    "openai": None,                      # API compatible OpenAI : modèle à préciser
    "fake": "fake",
}


@dataclass(frozen=True)
class Settings:
    # Fournisseur du LLM : "gemini", "claude", "openai" (toute API compatible OpenAI :
    # Ollama en local, Mistral, Groq…) ou "fake" (réponses factices, sans clé ni coût).
    llm: str = "gemini"
    model: str | None = None            # None : DEFAULT_MODELS[llm]
    effort: str = "low"                 # profondeur de réflexion : low, medium, high
    max_tokens: int = 2048              # la réflexion du modèle compte dans cette limite
    llm_timeout_s: float = 20.0
    openai_base_url: str | None = None  # ex. http://127.0.0.1:11434/v1 pour Ollama
    openai_api_key: str | None = None
    tokens_file: Path = SERVER_DIR / "config" / "tokens.txt"
    servers_file: Path = SERVER_DIR / "config" / "servers.toml"
    log_file: Path = SERVER_DIR / "data" / "logs" / "queries.jsonl"
    rate_limit: int = 10                # questions max par joueur…
    rate_window_s: int = 60             # …sur cette fenêtre glissante
    max_question_chars: int = 256       # limite du chat Minecraft

    @property
    def model_name(self) -> str:
        model = self.model or DEFAULT_MODELS.get(self.llm)
        if not model:
            raise ValueError(f"ASSIST_MODEL est obligatoire avec ASSIST_LLM={self.llm}")
        return model


def _path(value: str | None, default: Path) -> Path:
    """Chemin relatif = relatif au dossier server/, d'où qu'on lance l'API."""
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else SERVER_DIR / path


def from_env(env_file: Path = ENV_FILE) -> Settings:
    # Charge .env dans os.environ (sans écraser l'existant) : les SDK y lisent aussi
    # leurs clés (GEMINI_API_KEY, ANTHROPIC_API_KEY).
    load_dotenv(env_file, override=False)
    env = os.environ
    return Settings(
        llm=env.get("ASSIST_LLM", Settings.llm),
        model=env.get("ASSIST_MODEL") or None,
        effort=env.get("ASSIST_EFFORT", Settings.effort),
        openai_base_url=env.get("ASSIST_OPENAI_BASE_URL") or None,
        openai_api_key=env.get("ASSIST_OPENAI_API_KEY") or None,
        tokens_file=_path(env.get("ASSIST_TOKENS_FILE"), Settings.tokens_file),
        log_file=_path(env.get("ASSIST_LOG_FILE"), Settings.log_file),
        rate_limit=int(env.get("ASSIST_RATE_LIMIT", Settings.rate_limit)),
        rate_window_s=int(env.get("ASSIST_RATE_WINDOW_S", Settings.rate_window_s)),
    )
