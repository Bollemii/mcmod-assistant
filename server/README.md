# Serveur assistant

API HTTP de `/assist` (phase 1 : réponse d'un LLM, sans RAG), et scripts d'ingestion.

## Installation

Python 3.13+.

```bash
pip install -r requirements.txt
```

Toutes les commandes ci-dessous se lancent **depuis ce dossier `server/`**.

## Configuration : le fichier `.env`

Les clés et réglages se mettent dans **`server/.env`**. Copie le modèle, puis remplis-le :

```bash
cp .env.example .env
```

Pour Gemini, il suffit d'une ligne :

```
GEMINI_API_KEY=<ta clé>
```

- `server/.env` est ignoré par git (`.gitignore`) : c'est le seul endroit où mettre tes clés.
  `.env.example` est versionné et ne contient aucune clé.
- L'API le relit à chaque démarrage, d'où qu'on la lance.
- Une variable déjà définie dans l'environnement (PowerShell : `$env:ASSIST_LLM = "fake"`)
  l'emporte sur le fichier : pratique pour un essai ponctuel.
- Les chemins relatifs (`ASSIST_TOKENS_FILE`, `ASSIST_LOG_FILE`) partent du dossier `server/`.

## Choisir le LLM

Le fournisseur se choisit avec `ASSIST_LLM` dans `.env`.

| `ASSIST_LLM` | Coût | Clé | Modèle par défaut |
|---|---|---|---|
| `gemini` (défaut) | **gratuit** dans les quotas du palier gratuit | `GEMINI_API_KEY`, à créer sur [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `gemini-3.5-flash-lite` |
| `claude` | facturé à l'usage : l'API Claude **n'est pas incluse** dans l'abonnement Pro | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| `openai` | selon le service ; **gratuit en local avec Ollama** | `ASSIST_OPENAI_API_KEY` si le service en demande une | à préciser avec `ASSIST_MODEL` |
| `fake` | gratuit | aucune | réponses factices, pour tester le client |

**Gemini** : sur le palier gratuit, les quotas (questions par minute et par jour) sont
affichés dans AI Studio, et Google peut utiliser les questions envoyées pour améliorer
ses produits. Un quota dépassé donne une erreur 503 côté joueur (« assistant
indisponible »). `gemini-3.8-flash` donne de meilleures réponses mais a un quota gratuit
plus bas : `ASSIST_MODEL=gemini-3.8-flash`.

**API compatible OpenAI**, par exemple Ollama en local :

```
ASSIST_LLM=openai
ASSIST_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
ASSIST_MODEL=<modèle installé dans Ollama>
```

Le même réglage marche pour Mistral, Groq ou OpenRouter : leur URL d'API dans
`ASSIST_OPENAI_BASE_URL` et leur clé dans `ASSIST_OPENAI_API_KEY`.

### Toutes les variables

| Variable | Défaut | Rôle |
|---|---|---|
| `ASSIST_LLM` | `gemini` | Fournisseur : `gemini`, `claude`, `openai` ou `fake` |
| `ASSIST_MODEL` | selon le fournisseur | Modèle (obligatoire avec `openai`) |
| `ASSIST_EFFORT` | `low` | Profondeur de réflexion : `low`, `medium`, `high` (Gemini et Claude) |
| `GEMINI_API_KEY` | — | Clé Gemini |
| `ANTHROPIC_API_KEY` | — | Clé Claude |
| `ASSIST_OPENAI_BASE_URL` / `ASSIST_OPENAI_API_KEY` | — | API compatible OpenAI : URL et clé éventuelle |
| `ASSIST_RATE_LIMIT` / `ASSIST_RATE_WINDOW_S` | `10` / `60` | Questions max par joueur sur la fenêtre (secondes) |
| `ASSIST_TOKENS_FILE` | `config/tokens.txt` | Fichier des jetons |
| `ASSIST_LOG_FILE` | `data/logs/queries.jsonl` | Journal des questions (servira au jeu d'évaluation) |

## Jetons des joueurs

Chaque joueur a son propre jeton. Le fichier `config/tokens.txt` ne garde que son
empreinte : le jeton n'est affiché qu'une fois, à sa création.

```bash
python -m api.tokens new "Pseudo du joueur"
```

```bash
python -m api.tokens list
```

Un jeton ajouté (ou une ligne supprimée dans `tokens.txt`) prend effet sans redémarrer.

## Lancer l'API

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Au démarrage, une ligne `LLM : gemini, modèle gemini-3.5-flash-lite` confirme le
fournisseur lu dans `.env`.

Pour un test sans aucune clé (PowerShell) :

```powershell
$env:ASSIST_LLM = "fake"; python -m uvicorn api.main:app --port 8000
```

Vérifier que l'API répond :

```bash
curl http://127.0.0.1:8000/v1/health
```

Le contrat de l'API est décrit dans [docs/architecture.md](../docs/architecture.md#contrat-de-lapi).
Une documentation interactive est servie sur `http://127.0.0.1:8000/docs`.

## Tests

```bash
python -m unittest discover -s tests -t .
```

```bash
python -m unittest discover -s qa
```

## Ingestion

```bash
python ingestion/excalia_wiki.py
```

```bash
python ingestion/excalia_patch_notes.py
```

Les résultats sont écrits dans `data/servers/excalia/`.
