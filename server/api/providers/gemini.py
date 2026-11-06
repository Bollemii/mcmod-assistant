"""Gemini (API Gemini de Google). Clé : GEMINI_API_KEY, créée sur https://aistudio.google.com/apikey.

Le palier gratuit couvre les modèles Flash et Flash-Lite, avec des quotas par minute et
par jour (visibles dans AI Studio). Attention : sur le palier gratuit, Google peut
utiliser les questions envoyées pour améliorer ses produits.
"""
from google import genai
from google.genai import errors, types

from ..llm import REFUSAL_TEXT, SYSTEM_PROMPT, Answer, LLMUnavailable, build_user_message
from ..servers import ServerInfo
from ..settings import Settings

# Effort générique (ASSIST_EFFORT) -> niveau de réflexion Gemini.
THINKING_LEVELS = {
    "minimal": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}
BLOCKED = {types.FinishReason.SAFETY, types.FinishReason.PROHIBITED_CONTENT,
           types.FinishReason.BLOCKLIST, types.FinishReason.SPII}


class GeminiAnswerer:
    def __init__(self, settings: Settings, client: genai.Client | None = None):
        self.settings = settings
        self._client = client
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=settings.max_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_level=THINKING_LEVELS.get(settings.effort, types.ThinkingLevel.LOW)),
        )

    @property
    def client(self) -> genai.Client:
        """Créé au premier appel : sans clé, l'API démarre quand même et répond 503."""
        if self._client is None:
            try:
                # La clé est lue dans GEMINI_API_KEY (ou GOOGLE_API_KEY). Délai en millisecondes.
                self._client = genai.Client(
                    http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_s * 1000)))
            except ValueError as e:
                raise LLMUnavailable(f"client Gemini impossible à créer (GEMINI_API_KEY définie ?) : {e}") from e
        return self._client

    async def answer(self, question: str, server: ServerInfo | None, mc_version: str | None) -> Answer:
        model = self.settings.model_name
        try:
            response = await self.client.aio.models.generate_content(
                model=model, contents=build_user_message(question, server, mc_version), config=self.config)
        except errors.APIError as e:   # 429 (quota du palier gratuit atteint), 5xx, clé invalide…
            raise LLMUnavailable(f"Gemini {e.code} : {e.message}") from e

        usage = response.usage_metadata
        tokens = {"input_tokens": (usage.prompt_token_count or 0) if usage else 0,
                  "output_tokens": ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
                  if usage else 0}
        blocked_prompt = response.prompt_feedback and response.prompt_feedback.block_reason
        finish = response.candidates[0].finish_reason if response.candidates else None
        if blocked_prompt or finish in BLOCKED:
            return Answer(REFUSAL_TEXT, model, refused=True, **tokens)

        parts = response.candidates[0].content.parts if response.candidates and response.candidates[0].content else []
        text = " ".join(p.text.strip() for p in parts or [] if p.text and not p.thought).strip()
        if not text:
            raise LLMUnavailable(f"réponse vide (finish_reason={finish})")
        return Answer(text, response.model_version or model, **tokens)
