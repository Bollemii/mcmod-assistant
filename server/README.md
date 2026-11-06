# Serveur assistant

API HTTP de `/assist`, scripts d'ingestion et base de recherche (RAG).

Une question passe par trois étapes : recherche des extraits utiles dans le wiki et les
notes de patch du serveur (PostgreSQL + pgvector), réponse du LLM à partir de ces
extraits, puis liens cliquables vers les pages citées.

## Installation

Python 3.13+.

```bash
pip install -r requirements.txt
```

Toutes les commandes ci-dessous se lancent **depuis ce dossier `server/`**.

## Base de données

Docker suffit :

```bash
docker compose up -d
docker compose exec ollama ollama pull bge-m3      # une seule fois (1,2 Go)
```

- PostgreSQL 17 + pgvector écoute sur `127.0.0.1:5433` ; le schéma (`db/schema.sql`)
  est appliqué au premier démarrage.
- Ollama écoute sur `127.0.0.1:11434` et calcule les embeddings en local avec `bge-m3`
  (multilingue, 1024 dimensions) : **aucun quota, aucun coût**.

Puis on charge le contenu déjà ingéré :

```bash
python -m db.load
```

Ensuite, seuls les textes nouveaux ou modifiés sont recalculés. Si Ollama est arrêté,
la recherche continue en plein texte seul.

Les embeddings de Gemini restent possibles (`ASSIST_EMBED=gemini`) mais le palier
gratuit les limite à 1 000 textes par jour : un chargement complet (~950 textes) prend
presque tout le quota du jour.

Sans `ASSIST_DATABASE_URL` (ou si la base est arrêtée), l'API répond quand même, sans
extraits.

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
| `ASSIST_LOG_FILE` | `data/logs/queries.jsonl` | Journal des questions (aussi enregistrées dans la table `queries`) |
| `ASSIST_DATABASE_URL` | — | Base PostgreSQL + pgvector ; vide = pas de RAG |
| `ASSIST_EMBED` / `ASSIST_EMBED_MODEL` | `ollama` / `bge-m3` | Embeddings : `ollama` (local), `gemini`, `openai` (autre API compatible) ou `fake` |
| `ASSIST_EMBED_URL` | `http://127.0.0.1:11434/v1` | Adresse de l'API d'embeddings (`ollama` / `openai`) |
| `ASSIST_EMBED_THREADS` | `8` | Threads de calcul d'Ollama. Tous les cœurs (défaut d'Ollama) est bien plus lent sous Docker : 3 s par question au lieu de 0,04 s sur une machine à 14 cœurs |
| `ASSIST_TOP_K` | `8` | Extraits donnés au LLM (3 au plus par page) |
| `ASSIST_REFRESH_DAYS` | `7` | Rythme de mise à jour par défaut (jours) ; `refresh_days` de chaque source dans `servers.toml` le remplace |
| `ASSIST_REFRESH_COOLDOWN_MIN` | `60` | Délai minimal entre deux `/assist maj` d'une même source |
| `ASSIST_REFRESH_CHECK_S` | `3600` | Fréquence de vérification des sources à mettre à jour ; `0` = jamais automatiquement |

## Jetons des joueurs

Chaque joueur a son propre jeton. Le fichier `config/tokens.txt` ne garde que son
empreinte : le jeton n'est affiché qu'une fois, à sa création.

```bash
python -m api.tokens new "Pseudo du joueur"
```

Deux types de jetons :

| Type | Commandes |
|---|---|
| `joueur` | `/assist <question>`, `/assist maj` (au plus une mise à jour par source et par heure, tous joueurs confondus) |
| `admin` | en plus : `/assist maj force` (sans délai, tout est retéléchargé et rechargé) et `/assist sources` |

```bash
python -m api.tokens new --admin "Pseudo"
```

Les jetons créés avant la phase 2 (lignes sans type) restent valides comme jetons `joueur`.

```bash
python -m api.tokens list
```

Un jeton ajouté (ou une ligne supprimée dans `tokens.txt`) prend effet sans redémarrer.

## Lancer l'API

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Pour exposer l'API sur le réseau local (LAN), utiliser `--host 0.0.0.0`.

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

## Mise à jour des sources

- **Automatique** : l'API vérifie au démarrage puis toutes les heures ; une source mise à
  jour il y a plus de `refresh_days` jours (7 par défaut) est relancée en tâche de fond.
- **À la demande** : `/assist maj` en jeu (`POST /v1/refresh`).
- **À la main** :

```bash
python ingestion/excalia_wiki.py
python -m db.load --server excalia --source wiki
```

Une mise à jour est légère : le wiki Excalia tient en un appel d'API, les notes de patch
déjà connues ne sont pas retéléchargées, et seules les pages modifiées sont rechargées.

## Évaluation

Le jeu de questions est dans [`eval/excalia.jsonl`](eval/excalia.jsonl) (FAQ reformulée,
autres pages du wiki, notes de patch, questions hors sujet).

```bash
python -m eval.run_eval --retrieval-only     # recherche seule, sans LLM ni coût
python -m eval.run_eval                      # recherche, réponse et jugement par un LLM
```

L'évaluation complète fait deux appels au LLM par question (réponse et jugement), soit
~110 appels : sur Gemini, elle consomme le quota du vrai service. Voir
[AGENTS.md](../AGENTS.md).

## Tests

```bash
python -m unittest discover -s tests -t .
```

Les tests d'intégration avec la base sont ignorés sans base de test :

```powershell
$env:ASSIST_TEST_DATABASE_URL = "postgresql://assist:assist@127.0.0.1:5433/assist_test"
python -m unittest tests.test_rag_db
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
