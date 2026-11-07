# Plan de développement

Principe : **obtenir une chaîne complète au plus tôt**, puis l'enrichir source par
source en mesurant la qualité à chaque étape. Chaque phase se termine par un
critère de fin vérifiable.

| Phase | Objectif | État |
|---|---|---|
| 0 | Fondations : arborescence, ingestion Excalia (wiki + notes de patch), multi-serveur, confiance | ✅ terminé |
| 1 | Chaîne de bout en bout minimale (sans RAG) | ✅ terminé (testé en jeu avec Gemini) |
| 2 | RAG sur le contenu Excalia + jeu d'évaluation | code ✅, évaluation des réponses à faire |
| 3 | Connaissances Minecraft vanilla (wiki + recettes) | à faire |
| 4 | Apprentissage autonome à partir du chat | à faire |
| 5 | Qualité, coûts et exploitation | à faire |

---

## Phase 0 — Fondations ✅

- [x] Architecture validée ([architecture.md](architecture.md)).
- [x] Arborescence du projet (`docs/`, `server/`, `client/`).
- [x] Ingestion du wiki Excalia via `GET /api/wiki`
      ([`excalia_wiki.py`](../server/ingestion/excalia_wiki.py)) : 128 pages,
      818 chunks (avec le texte alternatif des images), détection des changements par hash.
- [x] Ingestion des notes de patch Excalia via `/api/patch-notes`
      ([`excalia_patch_notes.py`](../server/ingestion/excalia_patch_notes.py)) : liste
      API + sitemap (29 notes), 10 notes de la saison 3, 132 chunks datés.
- [x] Découpage HTML partagé ([`html_chunker.py`](../server/ingestion/html_chunker.py)).
- [x] Multi-serveur : configuration par serveur ([`servers.toml`](../server/config/servers.toml)),
      données rangées par serveur, règle « `server_id NULL` = commun » dans le schéma.
- [x] Schéma de base prévu et testé sur PostgreSQL + pgvector
      ([schema-donnees.md](schema-donnees.md), [`schema.sql`](../server/db/schema.sql)).
- [x] Système de confiance du chat conçu ([confiance-chat.md](confiance-chat.md)) et
      calcul implémenté avec 17 scénarios testés ([`trust.py`](../server/qa/trust.py)).

## Phase 1 — Chaîne de bout en bout minimale ✅

Objectif : une question tapée en jeu reçoit une réponse du LLM, sans RAG pour l'instant.
Cette phase lève les risques d'installation côté client avant d'investir dans le reste.

**Bilan de l'essai en jeu** : tout fonctionne avec Gemini. L'assistant répond
correctement et renvoie vers le wiki quand il ne sait pas répondre précisément.

- [x] **Vérifier le client** : le launcher Excalia utilise Fabric par défaut, et on peut
      y ajouter ChatTriggers (`ctjs`) directement. Aucun autre launcher n'est nécessaire.
      Procédure : [client/README.md](../client/README.md).
- [x] **Vérifier le règlement** d'Excalia : le module n'enfreint aucune clause et il est
      **validé par le staff**.
- [x] Module ChatTriggers [`client/assist/`](../client/assist/index.js) :
  - [x] commande `/assist <question>`, et `/assistconfig url|token|show` ;
  - [x] appel HTTP **asynchrone** (`java.net.http` dans un thread, affichage renvoyé sur
        le thread du jeu par `Client.scheduleTask`) ;
  - [x] message d'attente, réponse, sources cliquables (prêtes pour la phase 2), un
        message clair par code d'erreur (401, 422, 429, 503, serveur injoignable) ;
  - [x] configuration (URL de l'API, jeton) dans `config.json` ;
  - [x] 7 tests de la logique sous Node, avec des doublures de ChatTriggers
        ([`run_module.js`](../client/tests/run_module.js)).
- [x] API [`server/api/`](../server/api/main.py) : `POST /v1/ask` qui appelle un LLM
      au choix (Gemini par défaut, gratuit ; Claude ; toute API compatible OpenAI comme
      Ollama), `GET /v1/health`,
      jeton par joueur (empreintes seulement, `python -m api.tokens new`), limite de
      10 questions par minute, reconnaissance du serveur par son adresse, journal des
      questions (`data/logs/queries.jsonl`), mode factice sans clé (`ASSIST_LLM=fake`).
      25 tests, plus un essai HTTP réel en mode factice et une vérification des requêtes
      réelles des SDK Gemini et Claude contre un faux serveur local.
- [x] [`server/requirements.txt`](../server/requirements.txt) et instructions de
      lancement ([server/README.md](../server/README.md)).
- [x] Configuration par `server/.env` (modèle : `.env.example`, ignoré par git).
- [x] Premier appel réel à Gemini avec une clé gratuite (`GEMINI_API_KEY` dans `server/.env`), et mesure
      de la latence.

**Critère de fin** : `/assist Comment créer mon île ?` affiche une réponse en jeu en
moins de 5 s, et le jeu ne se fige pas pendant l'attente.

## Phase 2 — RAG sur le contenu Excalia

Objectif : des réponses fondées sur le wiki et les notes de patch, avec leurs sources.

- [x] PostgreSQL + pgvector et Ollama en local (`docker-compose.yml`), `schema.sql`
      appliqué au premier démarrage.
- [x] Modèle d'embeddings : **`bge-m3` via Ollama**, en local (multilingue, 1024
      dimensions, sans quota). Les embeddings Gemini ont été écartés : leur palier
      gratuit est limité à 1 000 textes par jour, soit à peine un chargement du wiki.
- [x] Chargeur `servers.toml` → `servers`, `server_addresses`, `sources`, `chat_roles`
      ([`db/load.py`](../server/db/load.py)).
- [x] Chargeur `chunks.jsonl` → base, incrémental (pages nouvelles, modifiées ou
      disparues), qui réutilise le vecteur de tout texte inchangé (`text_hash`).
- [x] Recherche hybride ([`retrieval.py`](../server/api/retrieval.py)) : vecteurs + plein
      texte pondéré par la rareté des mots (IDF), fusion RRF, filtres serveur / saison /
      expiration, 3 extraits au plus par page. Si les embeddings sont indisponibles,
      plein texte seul.
- [x] Prompt RAG : extraits numérotés et datés, « je ne sais pas » sinon, 1 à 4 phrases,
      note de patch la plus récente prioritaire, ligne finale `SOURCES: 1, 3`.
- [x] Réponse de l'API avec `sources` (pages citées, sinon le wiki) ; liens cliquables
      dans le client.
- [x] Journalisation dans `queries` (en plus de `queries.jsonl`).
- [x] **Jeu d'évaluation** ([`eval/excalia.jsonl`](../server/eval/excalia.jsonl)) :
      55 questions écrites comme un joueur (30 de la FAQ, 15 d'autres pages du wiki,
      6 de notes de patch, 4 hors sujet ou sans réponse, 1 tentative de détournement).
- [x] Script d'évaluation ([`eval/run_eval.py`](../server/eval/run_eval.py)), qui mesure :
  - la bonne source dans le top 8 (*recall@8*) ;
  - la justesse de la réponse ;
  - la latence ;
  - le coût par question.
- [ ] **Évaluation des réponses** (justesse, latence, coût) : à lancer avec l'accord du
      propriétaire, car elle fait ~110 appels au LLM (voir [CLAUDE.md](../CLAUDE.md)).
- [ ] Réglage de l'effort du LLM (`low` par défaut) et, si besoin, comparaison avec
      un modèle moins cher, **à partir des mesures**.

### Corrections après l'essai en jeu (07/11)

- [x] Base injoignable (Docker arrêté) : le LLM n'est plus appelé, la réponse renvoie
      vers le wiki ; message « Docker est-il lancé ? » dans les journaux de l'API.
- [x] Temps de recherche et temps du LLM enregistrés séparément (`search_ms`, `llm_ms`).
- [x] Extraits utilisés enregistrés aussi par leur clé stable (`queries.chunk_keys`),
      qui survit au rechargement de la base. Migration automatique des bases existantes.

### Mode allégé (07/11)

- [x] Essai de LLM locaux sur processeur (Ollama, Core Ultra 5 sans carte graphique) :
      `gemma3:4b` 9 réponses justes sur 10 en ~24 s, `ministral-3:3b` 6/10, `qwen3:4b`
      inutilisable. Goulot : la lecture du prompt (~70 tokens/s).
- [x] Gemini reste le fournisseur principal, avec son prompt complet. S'il échoue,
      bascule vers `gemma3:4b` avec un prompt allégé (4 extraits de 700 caractères) ;
      réponse marquée « mode allégé » en jeu ; Gemini mis en pause selon l'erreur.
- [x] Client : attente portée à 90 s.

### Mise à jour des sources

Les wikis des serveurs et les notes de patch sont mis à jour de façon **légère** :
seules les pages nouvelles, modifiées ou supprimées sont traitées. Le wiki Excalia
tient en un seul appel à `/api/wiki`, et les notes de patch déjà connues ne sont pas
retéléchargées. Les embeddings ne sont recalculés que pour les pages modifiées.

- [x] Date de dernière mise à jour par source (colonne `refreshed_at` dans `sources`).
- [x] **Mise à jour automatique hebdomadaire** : au démarrage de l'API, puis toutes les
      heures, chaque source dont la dernière mise à jour date de plus de `refresh_days`
      jours est relancée en tâche de fond.
  - Paramétrable par source dans `servers.toml` (`refresh_days = 7`), avec une valeur
    par défaut globale (`ASSIST_REFRESH_DAYS=7`). `refresh_days = 0` désactive la
    mise à jour automatique.
  - Le **wiki Minecraft** est exclu : il est volumineux et ne change qu'avec la version
    du jeu. Il est mis à jour à la main, lors d'une montée de version (phase 3).
- [x] **Mise à jour à la demande par les joueurs** : `/assist maj` relance la mise à jour
      légère des sources du serveur où se trouve le joueur.
  - Délai minimal entre deux mises à jour d'une même source, commun à tous les joueurs
    (`ASSIST_REFRESH_COOLDOWN_MIN=60`). Pendant ce délai, la commande indique la date
    de la dernière mise à jour au lieu de la relancer.
  - Réponse en jeu : « mise à jour lancée », puis le bilan (pages ajoutées, modifiées,
    supprimées).
- [x] **Deux types de jetons** :

  | Type | Commandes |
  |---|---|
  | `joueur` | `/assist <question>`, `/assist maj` (soumis au délai) ; plus tard les notes 👍 / 👎 (phase 5) |
  | `admin` | tout ce que fait `joueur`, plus `/assist maj force` (sans délai, tout est retéléchargé et rechargé), `/assist sources` (état et date de chaque source), et plus tard les commandes d'administration de la phase 4 (épingler / bannir un fait) |

  Le type est une colonne de `tokens.txt` (`joueur` par défaut, ce qui garde les jetons
  actuels valides) ; création par `python -m api.tokens new --admin "Pseudo"`. L'API
  répond `403` à une commande avancée envoyée avec un jeton `joueur`.

**Mesures de la recherche** (06/11/2026, 51 questions avec source attendue, sans LLM) :

| Mode | Bonne page en 1ʳᵉ position | dans le top 3 | dans le top 8 | Temps (médiane) |
|---|---|---|---|---|
| Hybride, `bge-m3` (Ollama) + plein texte IDF | **92 %** | **100 %** | **100 %** | 65 ms |
| Plein texte seul, pondéré IDF (si Ollama est arrêté) | 78 % | 88 % | 94 % | 18 ms |
| Plein texte seul, sans IDF (première version) | 39 % | 57 % | 80 % | 9 ms |

Ollama doit tourner avec 8 threads (`ASSIST_EMBED_THREADS`) : avec les 14 par défaut,
l'embedding d'une question prend 3 s au lieu de 0,04 s.

**Critère de fin** : au moins 80 % de réponses justes sur le jeu d'évaluation, la
bonne source dans le top 8 pour au moins 90 % des questions (✅ 100 %), et « je ne sais pas »
plutôt qu'une invention sur les questions hors sujet.

## Phase 3 — Connaissances Minecraft vanilla

Objectif : répondre aux questions de jeu de base (crafts, mécaniques), en laissant
la priorité aux règles du serveur. Exemple réel du 07/11 : « comment craft une table
d'enchant ? » a reçu la recette de mémoire, sans préciser que l'équipement Excalia ne
s'enchante pas.

- [x] Version de Minecraft du serveur : **26.2**.
- [ ] **Sources locales d'abord** : le launcher a déjà installé le jeu dans
      `.minecraft` (jar `versions/26.2/26.2.jar`, index `assets/indexes/*.json` et
      `assets/objects/`). Si les fichiers y sont, aucun téléchargement. Sinon,
      téléchargement depuis les serveurs de Mojang (~37 Mo, **avec accord**, voir CLAUDE.md).
- [ ] Data version de 26.2 (`version.json` du jar, champ `world_version`) → `mc_versions`.
- [ ] Extraction :
  - [ ] recettes (`data/minecraft/recipe/*.json` du jar : forme, sans forme, cuisson,
        forgeron, tailleur de pierre) → `items`, `recipes` ;
  - [ ] noms des objets : `en_us.json` (dans le jar) et `fr_fr.json` (dans les assets)
        → `item_aliases`, avec les formes sans accent et au pluriel.
- [ ] Détection d'entités : les mots de la question sont comparés aux alias (« table
      d'enchant » → `minecraft:enchanting_table`), en tolérant les fautes et les
      abréviations courantes ; les recettes trouvées sont injectées dans le prompt,
      avant les extraits du wiki.
- [ ] Priorité au serveur :
  - [ ] les recettes et règles du serveur trouvées dans le wiki Excalia passent avant
        la recette vanilla ;
  - [ ] liste par serveur des objets désactivés ou modifiés (`recipes.disabled`,
        `servers.toml`) ;
  - [ ] consigne explicite dans le prompt (« recette vanilla, peut différer sur le serveur »).
- [ ] Wiki Minecraft FR (API MediaWiki), limité aux pages utiles (blocs, objets,
      mécaniques), licence CC BY-NC-SA citée. Volume à estimer avant tout appel :
      **scraping soumis à accord** (CLAUDE.md). À décider après les étapes précédentes :
      les recettes seules couvrent peut-être l'essentiel.
- [ ] Jeu d'évaluation : ~20 questions vanilla (recettes, cuisson, mécaniques) et
      ~10 questions piège (objet modifié ou interdit par Excalia), mesurées d'abord en
      recherche seule (local, sans LLM).

**Critère de fin** : `/assist Comment craft un four ?` renvoie la recette exacte, et
une question sur un objet modifié par Excalia (table d'enchantement, four nucléaire)
renvoie la version du serveur.

## Phase 4 — Apprentissage autonome à partir du chat

Objectif : enrichir la base avec les réponses que les joueurs se donnent entre eux,
**sans validation humaine obligatoire**, sans y faire entrer d'erreurs. Conception :
[confiance-chat.md](confiance-chat.md).

- [ ] Choisir la méthode de collecte :
  - **logs client** (`logs/latest.log`, `logs/*.log.gz`) : historique déjà disponible ;
  - **capture en direct** avec ChatTriggers (`register("chat", …)`) : format plus
    propre, mais seulement à partir de maintenant.
- [x] Relever le format réel du chat d'Excalia (07/11) : symbole de grade devant le
      pseudo (`ॐ Lorn87: …`), table `[excalia.chat.grades]` dans `servers.toml`
      (14 symboles ; `꒔` inconnu, Développeur à confirmer).
- [ ] Analyseur du chat : canaux publics seulement (MP exclus), rôle lu dans le préfixe,
      **pseudonymisation** (HMAC), écriture dans `players` et `chat_messages`.
- [ ] Extraction hors ligne par le LLM : question canonique, réponse canonique,
      position (soutient / contredit), certitude.
- [ ] Regroupement : rattacher chaque occurrence à un fait existant (question et
      réponse proches) ou en créer un, dans le bon `question_group`.
- [ ] Tâche de maintenance horaire : calcul de confiance ([`trust.py`](../server/qa/trust.py)),
      vérification officielle par le LLM, changements de statut, mise à jour des
      réputations, indexation ou retrait des faits actifs (source `chat_qa`).
- [ ] Revérification des faits actifs à chaque nouvelle note de patch proche.
- [ ] Prise en compte des 👍 / 👎 de `/assist` sur les faits utilisés (`queries.fact_ids`).
- [ ] Outil d'administration facultatif : épingler / bannir un fait, forcer un rôle,
      bannir un auteur.
- [ ] Calibrage des seuils sur des données réelles (part de faits activés, erreurs
      constatées).
- [ ] Rapport des questions fréquentes sans bonne réponse, à remonter à l'équipe
      Excalia pour compléter le wiki.

**Critère de fin** : les faits activés automatiquement améliorent le score sur un lot
de questions issues du chat, sans faire baisser le score du jeu d'évaluation de la
phase 2, et moins de 5 % des faits actifs contrôlés à la main sont faux.

## Phase 5 — Qualité, coûts et exploitation

- [ ] Cache sémantique : resservir une réponse récente à une question quasi identique.
- [ ] Retours joueurs `/assist bien` / `/assist faux`, puis revue des réponses notées 👎.
- [ ] Limite de débit par joueur et budget mensuel LLM, avec une alerte.
- [ ] Prompt caching du prompt système, vérifié via `cache_read_input_tokens`.
- [ ] Suivi : latence, coût par question, taux de « je ne sais pas », taux d'erreur.
- [ ] Hébergement du serveur assistant (petite VM ou conteneur) et sauvegarde de la base.
- [ ] Distribution du module (procédure d'installation, `/ct import` si publié).
- [ ] Changement de saison : mise à jour de `season` dans `servers.toml` ; les sources
      et les faits de l'ancienne saison sortent automatiquement de la recherche.
- [ ] Ajout d'un deuxième serveur, pour valider le fonctionnement multi-serveur.

---

## Risques

| Risque | Impact | Parade |
|---|---|---|
| ~~Le launcher Excalia n'accepte pas Fabric + ChatTriggers~~ | — | **Levé** : le launcher utilise Fabric par défaut et accepte `ctjs`. |
| ~~Le module est jugé non conforme au règlement~~ | — | **Levé** : validé par le staff. À reconfirmer si le module gagne des fonctions qui agissent en jeu. |
| Abus de `/assist maj` (relances en boucle) | Charge sur le site Excalia | Délai minimal par source commun à tous, mise à jour légère, reconstruction complète réservée aux jetons `admin`. |
| Les routes `/api/wiki` ou `/api/patch-notes` changent ou disparaissent | Ingestion cassée | Garder les réponses brutes (`wiki_raw.json`, `raw/`) comme dernière copie connue. Le site expose aussi un sitemap et des pages HTML exploitables. |
| La saison 3 est « en préparation », le wiki va beaucoup bouger | Réponses obsolètes | Mise à jour légère hebdomadaire (paramétrable) et à la demande avec `/assist maj`, chunks d'une page modifiée remplacés, date de mise à jour affichée. |
| Les Q/R du chat sont fausses ou périmées | Mauvaises réponses | Système de confiance : une voix par auteur, rôles, réputation, vérification officielle, priorité basse, expiration. |
| Manipulation du chat (plusieurs comptes qui répètent une fausse réponse) | Fait faux activé | Confiance faible des joueurs (6 auteurs distincts nécessaires), rejet si une source officielle contredit, 👎 des joueurs, bannissement d'auteurs. |
| Coût du LLM si le module est diffusé largement | Budget dépassé | Gemini gratuit par défaut, jeton par joueur, limite de débit, cache, effort `low`, budget avec alerte en cas de fournisseur payant. |
| Quotas du palier gratuit Gemini dépassés | Réponses en 503 aux heures chargées | Cache sémantique (phase 5), modèle Flash-Lite (quota plus haut), bascule vers Ollama en local ou un palier payant. |

## Prochaine étape

Phase 3, en commençant par **vérifier ce que le launcher a déjà installé dans
`.minecraft`** (jar 26.2 et fichiers de langue) : si tout y est, l'extraction des
recettes et des noms français se fait sans aucun téléchargement.

Reste de la phase 2 : l'évaluation de la justesse des réponses (~110 appels au LLM),
à lancer avec l'accord du propriétaire.
