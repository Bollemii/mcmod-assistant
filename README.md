# mcmod-assistant

Assistant IA en jeu pour Minecraft : `/assist Comment craft un four ?` renvoie une
réponse courte et sourcée, fondée sur le wiki et les notes de patch du serveur
(d'abord **Excalia — Skyblock saison 3**, Minecraft 26.2), sur les connaissances
Minecraft vanilla et sur ce que les joueurs se répondent dans le chat. Un même
déploiement sert plusieurs serveurs.

## Organisation

```
mcmod-assistant/
├── docs/                    Documentation
│   ├── architecture.md        Composants, flux d'une question, contrat d'API, choix techniques
│   ├── schema-donnees.md      Base PostgreSQL + pgvector, multi-serveur, recherche hybride
│   ├── confiance-chat.md      Apprentissage depuis le chat et système de confiance
│   └── plan-developpement.md  Phases, critères de fin, risques
├── server/                  Serveur assistant (Python) — voir server/README.md
│   ├── api/                   API HTTP /v1/ask, jetons, limite de débit
│   ├── tests/                 Tests de l'API
│   ├── config/servers.toml    Serveurs pris en charge : adresses, sources, rôles du chat
│   ├── db/schema.sql          Création de la base
│   ├── ingestion/             Récupération et découpage des sources
│   │   ├── excalia_wiki.py      Wiki Excalia -> chunks
│   │   ├── excalia_patch_notes.py  Notes de patch Excalia -> chunks
│   │   ├── html_chunker.py      Découpage HTML partagé
│   │   └── common.py            HTTP, configuration, sorties
│   ├── qa/                    Q/R du chat : calcul de confiance (trust.py) et ses tests
│   └── data/                  Données produites (non versionnées)
│       ├── common/              Contenu commun à tous les serveurs
│       └── servers/<slug>/      Contenu propre à un serveur
└── client/                  Module ChatTriggers — voir client/README.md
    ├── assist/                Module /assist (index.js, metadata.json)
    └── tests/                 Tests de la logique du module sous Node
```

## Démarrage rapide

Installer les dépendances (Python 3.13+) :

```bash
pip install -r server/requirements.txt
```

Créer la configuration, puis y mettre la clé du LLM (`GEMINI_API_KEY`, gratuite, par
défaut ; Claude ou Ollama au choix, voir [server/README.md](server/README.md)) :

```bash
cp server/.env.example server/.env
```

Lancer l'API depuis `server/`, puis installer le module en jeu
([client/README.md](client/README.md)) :

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Récupérer et découper le contenu d'Excalia (saison 3) :

```bash
python server/ingestion/excalia_wiki.py
```

```bash
python server/ingestion/excalia_patch_notes.py
```

Les résultats sont écrits dans `server/data/servers/excalia/`. Pour tester le calcul de
confiance :

```bash
python -m unittest discover -s server/qa
```

## État

La phase 0 est terminée : architecture, ingestion du wiki et des notes de patch
d'Excalia, stockage multi-serveur, schéma de base et calcul de confiance du chat.
Le code de la phase 1 est écrit et testé (API `/v1/ask` et module `/assist`) ;
reste à l'essayer en jeu, avec une clé Gemini gratuite.
La suite est décrite dans [docs/plan-developpement.md](docs/plan-developpement.md).
