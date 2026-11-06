# Consignes pour les agents (IA) qui travaillent sur ce projet

## 1. Règle absolue : aucun appel à un service externe sans accord explicite

Les API de LLM et d'embeddings (Gemini, Claude, OpenAI, Mistral, Groq, OpenRouter…)
sont **payantes ou limitées par des quotas** que le propriétaire du projet utilise pour
le vrai service. Un agent ne doit **jamais** les appeler de lui-même.

- **Interdit sans accord explicite de l'utilisateur, donné pour cette action précise** :
  tout appel à une API de LLM ou d'embeddings distante, y compris « juste un essai »,
  un test de quota, une comparaison de modèles, une évaluation, un chargement de base
  ou le lancement de l'API configurée avec un fournisseur distant.
- **Avant de demander**, annoncer :
  - le nombre d'appels et le volume de texte (tokens) prévus ;
  - le quota concerné et la part qu'il en consommera ;
  - l'alternative locale possible.
- **Un accord ne vaut que pour ce qui a été annoncé.** Un nouveau lot, une relance ou
  un autre modèle demandent un nouvel accord.
- **Aucune tâche de fond** qui appelle un service externe ne doit rester en marche :
  l'arrêter dès qu'elle n'est plus nécessaire, et ne jamais en lancer en parallèle
  sans accord.
- **Test de quota** : tout test de quota sera refusé si l'agent ne peut pas le faire **en local** (Ollama, doublures, etc.). Le quota est limité et doit être préservé pour le vrai service.
- **Scraping** : tout scraping de sites externes est soumis à la même règle. Il est interdit de lancer un scraping automatique sans accord explicite, même pour un test ou un essai.

## 2. Pour le développement et les tests : tout en local

| Besoin | À utiliser | Réglage |
|---|---|---|
| Embeddings | **Ollama en Docker** (`bge-m3`, 1024 dimensions) | `ASSIST_EMBED=ollama` (défaut) |
| LLM | **Ollama en Docker** (modèle local) ou réponses factices | `ASSIST_LLM=openai` + `ASSIST_OPENAI_BASE_URL=http://127.0.0.1:11434/v1`, ou `ASSIST_LLM=fake` |
| Tests unitaires | doublures (`fake`) | aucun réseau |

- `docker compose up -d` (dans `server/`) lance PostgreSQL + pgvector et Ollama.
- Les tests (`python -m unittest discover -s tests -t .`, `node client/tests/run_module.js`)
  ne font **aucun** appel réseau. Tout nouveau test doit garder cette propriété.
- Le fichier `server/.env` du propriétaire peut pointer vers Gemini : **ne pas lancer**
  l'API, `db.load` ou `eval.run_eval` avec ce fichier sans vérifier `ASSIST_LLM` et
  `ASSIST_EMBED`. Pour forcer le local dans une commande :
  `ASSIST_LLM=fake ASSIST_EMBED=ollama python -m …`
- `python -m eval.run_eval --retrieval-only` n'appelle aucun LLM : c'est le seul mode
  d'évaluation autorisé sans accord (avec des embeddings locaux).

## 3. Quotas connus (palier gratuit Gemini, par modèle)

| Service | Limites |
|---|---|
| Embeddings (`gemini-embedding-*`) | 100 textes/min, 30 000 tokens/min, **1 000 textes/jour** |
| LLM (`gemini-*-flash*`) | voir AI Studio ; un dépassement rend `/assist` indisponible (503) pour les joueurs |

Un chargement complet du wiki Excalia représente ~950 textes à embedder : c'est presque
tout le quota journalier d'un modèle d'embeddings distant. D'où Ollama en local.

## 4. Sites externes

L'ingestion appelle le site Excalia (`/api/wiki`, `/api/patch-notes`, sitemap). Ces
appels sont légers mais ne doivent pas être répétés en boucle : une ingestion par essai,
et `--from-file` pour retravailler le découpage sans réseau.

## 5. Autres conventions

- Documentation et messages en **français**, tutoiement pour les joueurs.
- Toutes les commandes serveur se lancent depuis `server/`.
- Ne jamais committer ; jamais de clé ni de jeton dans un fichier versionné
  (`server/.env` et `server/config/tokens.txt` sont ignorés par git).
