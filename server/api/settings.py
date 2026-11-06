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

# Modèle d'embeddings par défaut (ASSIST_EMBED_MODEL le remplace). Changer de modèle
# impose de recalculer tous les vecteurs : python -m db.load --force
DEFAULT_EMBED_MODELS = {
    "ollama": "bge-m3",                  # local (docker-compose.yml), 1024 dimensions, aucun quota
    "gemini": "gemini-embedding-2",      # distant ; palier gratuit : 1 000 textes par jour
    "openai": None,
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

    # --- RAG (phase 2) ---
    # Base PostgreSQL + pgvector (docker-compose.yml). Vide : pas de RAG, l'API répond
    # comme en phase 1 avec les connaissances générales du LLM.
    database_url: str | None = None
    # Embeddings : "ollama" (défaut, local), "openai" (autre API compatible), "gemini" ou "fake".
    embed: str = "ollama"
    embed_url: str = "http://127.0.0.1:11434/v1"   # Ollama, ou autre API compatible OpenAI
    embed_threads: int = 8              # Ollama : threads de calcul (trop = plus lent sous Docker) ; 0 = défaut
    embed_model: str | None = None      # None : DEFAULT_EMBED_MODELS[embed]
    embed_dim: int = 1024               # doit correspondre à vector(1024) dans db/schema.sql
    top_k: int = 8                      # extraits donnés au LLM
    max_chunks_per_doc: int = 3         # pour varier les pages citées

    # --- Mise à jour des sources ---
    refresh_days: float = 7             # par défaut ; remplacé par refresh_days de chaque source
    refresh_cooldown_min: float = 60    # délai minimal entre deux /assist maj d'une même source
    refresh_check_s: int = 3600         # vérification périodique ; 0 = pas de mise à jour automatique

    @property
    def embed_model_name(self) -> str:
        model = self.embed_model or DEFAULT_EMBED_MODELS.get(self.embed)
        if not model:
            raise ValueError(f"ASSIST_EMBED_MODEL est obligatoire avec ASSIST_EMBED={self.embed}")
        return model

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
        database_url=env.get("ASSIST_DATABASE_URL") or None,
        embed=env.get("ASSIST_EMBED", Settings.embed),
        embed_model=env.get("ASSIST_EMBED_MODEL") or None,
        embed_url=env.get("ASSIST_EMBED_URL") or Settings.embed_url,
        embed_threads=int(env.get("ASSIST_EMBED_THREADS", Settings.embed_threads)),
        top_k=int(env.get("ASSIST_TOP_K", Settings.top_k)),
        refresh_days=float(env.get("ASSIST_REFRESH_DAYS", Settings.refresh_days)),
        refresh_cooldown_min=float(env.get("ASSIST_REFRESH_COOLDOWN_MIN", Settings.refresh_cooldown_min)),
        refresh_check_s=int(env.get("ASSIST_REFRESH_CHECK_S", Settings.refresh_check_s)),
    )
