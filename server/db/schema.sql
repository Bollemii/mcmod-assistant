-- Schéma de la base de l'assistant /assist (PostgreSQL 16+, pgvector 0.8+).
-- Explications : docs/schema-donnees.md (tables, multi-serveur)
--                docs/confiance-chat.md (Q/R du chat et système de confiance)
--
-- Règle multi-serveur : partout, server_id NULL = donnée commune à tous les serveurs
-- (wiki Minecraft, données du jeu) ; server_id renseigné = propre à ce serveur.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() n'est pas IMMUTABLE : on l'enveloppe pour pouvoir l'utiliser
-- dans des colonnes générées et des index.
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
  AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$;

-- Même problème pour array_to_string() (STABLE en général, sûr pour un text[]).
CREATE OR REPLACE FUNCTION f_join(text[]) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
  AS $$ SELECT array_to_string($1, ' ') $$;

-- ---------------------------------------------------------------------------
-- Référentiels : versions et serveurs
-- ---------------------------------------------------------------------------

-- Versions de Minecraft, comparées par "data version" entière
-- (lue dans le version.json du jar client, champ world_version).
CREATE TABLE mc_versions (
  data_version int PRIMARY KEY,
  name         text UNIQUE NOT NULL             -- '26.2'
);

-- Un serveur pris en charge ; reflète server/config/servers.toml.
CREATE TABLE servers (
  id               serial PRIMARY KEY,
  slug             text UNIQUE NOT NULL,        -- 'excalia' (clé de servers.toml)
  name             text NOT NULL,
  mc_data_version  int REFERENCES mc_versions(data_version),
  season           text,                        -- saison en cours ('saison3') ; NULL si sans saisons
  active           boolean NOT NULL DEFAULT true
);

-- Adresses par lesquelles le client désigne un serveur (plusieurs par serveur).
CREATE TABLE server_addresses (
  address    text PRIMARY KEY,                  -- en minuscules, sans port : 'play.excalia.fr'
  server_id  int NOT NULL REFERENCES servers(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Texte libre : sources -> documents -> chunks
-- ---------------------------------------------------------------------------

CREATE TYPE source_kind AS ENUM (
  'server_wiki',    -- wiki du serveur
  'server_news',    -- notes de patch, annonces du serveur
  'game_data',      -- données extraites du jeu (commun)
  'official_wiki',  -- wiki Minecraft (commun)
  'chat_qa'         -- Q/R du chat activées par le système de confiance
);

CREATE TABLE sources (
  id            serial PRIMARY KEY,
  kind          source_kind NOT NULL,
  server_id     int REFERENCES servers(id),     -- NULL = commune à tous les serveurs
  config_key    text NOT NULL,                  -- clé dans servers.toml ('wiki', 'patch_notes'…)
  season        text,                           -- saison couverte ; NULL = toutes
  priority      smallint NOT NULL,              -- 100 wiki serveur, 90 notes de patch, 70 données du jeu,
                                                -- 50 wiki Minecraft, 10 Q/R du chat
  lang          text NOT NULL DEFAULT 'fr',
  active        boolean NOT NULL DEFAULT true,
  refreshed_at  timestamptz,                    -- dernière mise à jour réussie (rythme hebdomadaire, /assist maj)
  UNIQUE NULLS NOT DISTINCT (kind, server_id, config_key, season)
);

CREATE TABLE documents (
  id            bigserial PRIMARY KEY,
  source_id     int NOT NULL REFERENCES sources(id),
  url           text NOT NULL,
  title         text NOT NULL,
  category      text,                           -- ex. 'La pêche', ou la version d'une note de patch
  content_hash  text NOT NULL,                  -- ne recalculer que si le contenu change
  published_at  timestamptz,                    -- date de la source (notes de patch) : la plus récente l'emporte
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz,                    -- surtout pour chat_qa
  UNIQUE (source_id, url)
);

CREATE TABLE chunks (
  id            bigserial PRIMARY KEY,
  document_id   bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_key     text UNIQUE NOT NULL,           -- id stable produit par l'ingestion
  url           text NOT NULL,                  -- avec ancre de section
  heading_path  text[] NOT NULL,                -- {'Les cannes (12)','Chiffres'}
  content       text NOT NULL,
  versions      int4range,                      -- plage de data versions ; NULL = toutes
  confidence    real NOT NULL DEFAULT 1.0,      -- < 1 pour les Q/R du chat
  embedding     vector(1024) NOT NULL,          -- dimension du modèle d'embeddings choisi
  embed_model   text NOT NULL,                  -- pour réindexer si on change de modèle
  text_hash     text NOT NULL,                  -- empreinte du texte embeddé : un chunk inchangé garde son vecteur
  tsv          tsvector GENERATED ALWAYS AS (
                  setweight(to_tsvector('french', f_unaccent(f_join(heading_path))), 'A') ||
                  setweight(to_tsvector('french', f_unaccent(content)), 'B')
                ) STORED,
  metadata      jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_idx       ON chunks USING gin (tsv);
CREATE INDEX chunks_versions_idx  ON chunks USING gist (versions);
CREATE INDEX chunks_document_idx  ON chunks (document_id);
CREATE INDEX chunks_text_hash_idx ON chunks (embed_model, text_hash);

-- ---------------------------------------------------------------------------
-- Données structurées : items et recettes
-- ---------------------------------------------------------------------------

CREATE TABLE items (
  id         text PRIMARY KEY,                  -- 'minecraft:furnace', 'excalia:four_nucleaire'
  server_id  int REFERENCES servers(id)         -- NULL = vanilla
);

-- Pont entre le langage naturel et les identifiants : fr_fr.json / en_us.json (communs),
-- puis argot propre à un serveur ('comp' = compacté sur Excalia).
CREATE TABLE item_aliases (
  id         bigserial PRIMARY KEY,
  item_id    text NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  server_id  int REFERENCES servers(id),        -- NULL = alias valable partout
  lang       text NOT NULL,
  alias      text NOT NULL,
  UNIQUE NULLS NOT DISTINCT (item_id, server_id, lang, alias)
);
CREATE INDEX item_aliases_alias_idx ON item_aliases (lower(f_unaccent(alias)));

CREATE TABLE recipes (
  id           bigserial PRIMARY KEY,
  result_item  text NOT NULL REFERENCES items(id),
  result_qty   smallint NOT NULL DEFAULT 1,
  type         text NOT NULL,                   -- crafting_shaped, smelting, ccraft_6x6…
  pattern      jsonb,                           -- grille, ingrédients, coût éventuel
  versions     int4range,
  server_id    int REFERENCES servers(id),      -- recette propre au serveur (prioritaire)
  disabled     boolean NOT NULL DEFAULT false,  -- recette vanilla désactivée par le serveur
  source_url   text                             -- page qui la décrit, si connue
);
CREATE INDEX recipes_result_idx ON recipes (result_item);

-- ---------------------------------------------------------------------------
-- Chat des joueurs et système de confiance (docs/confiance-chat.md)
-- ---------------------------------------------------------------------------

-- Confiance de base par rôle, par serveur ; reflète [<serveur>.chat.roles] de servers.toml.
CREATE TABLE chat_roles (
  server_id   int NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  role        text NOT NULL,                    -- 'admin', 'moderateur', 'guide', 'joueur'
  base_trust  real NOT NULL CHECK (base_trust BETWEEN 0 AND 1),
  PRIMARY KEY (server_id, role)
);

-- Un auteur du chat sur un serveur, identifié par HMAC de son pseudo (jamais en clair).
-- Sa réputation apprise (confirmed / contradicted) corrige la confiance de son rôle.
CREATE TABLE players (
  server_id     int NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  author_hash   text NOT NULL,
  role          text NOT NULL DEFAULT 'joueur',
  role_source   text NOT NULL DEFAULT 'prefix', -- 'prefix' (lu dans le chat) ou 'manual' (prioritaire)
  confirmed     int NOT NULL DEFAULT 0,         -- ses réponses devenues des faits actifs
  contradicted  int NOT NULL DEFAULT 0,         -- ses réponses rejetées ou contredites
  banned        boolean NOT NULL DEFAULT false, -- troll : confiance 0
  first_seen    timestamptz NOT NULL DEFAULT now(),
  last_seen     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (server_id, author_hash),
  FOREIGN KEY (server_id, role) REFERENCES chat_roles(server_id, role)
);

CREATE TABLE chat_messages (
  id           bigserial PRIMARY KEY,
  server_id    int NOT NULL REFERENCES servers(id),
  sent_at      timestamptz NOT NULL,
  author_hash  text NOT NULL,
  author_role  text NOT NULL,                   -- rôle lu dans le préfixe au moment du message
  channel      text,                            -- global, local, commerce… selon le format du serveur
  content      text NOT NULL,                   -- sans le préfixe ni le pseudo
  log_file     text NOT NULL,                   -- traçabilité
  UNIQUE (server_id, sent_at, author_hash, content),
  FOREIGN KEY (server_id, author_hash) REFERENCES players(server_id, author_hash)
);
CREATE INDEX chat_messages_time_idx ON chat_messages (server_id, sent_at);

-- Un « fait » : une question canonique et UNE réponse canonique. Plusieurs réponses
-- différentes à la même question = plusieurs faits concurrents (même question_group).
CREATE TYPE fact_status AS ENUM (
  'candidate',  -- pas encore assez de confiance
  'active',     -- indexé et utilisé pour répondre (automatique au-delà du seuil)
  'rejected',   -- contredit (par une source officielle ou plus fiable)
  'expired',    -- plus confirmé depuis longtemps, ou saison terminée
  'pinned',     -- forcé actif par un humain
  'banned'      -- forcé inactif par un humain
);
CREATE TYPE official_check AS ENUM ('unchecked', 'consistent', 'not_covered', 'contradicts');

CREATE TABLE qa_facts (
  id              bigserial PRIMARY KEY,
  server_id       int NOT NULL REFERENCES servers(id),
  season          text,
  question_group  bigint NOT NULL,              -- faits qui répondent à la même question
  question        text NOT NULL,
  answer          text NOT NULL,
  q_embedding     vector(1024) NOT NULL,
  a_embedding     vector(1024) NOT NULL,
  confidence      real NOT NULL DEFAULT 0,      -- recalculée par la maintenance (server/qa/trust.py)
  support_authors int NOT NULL DEFAULT 0,       -- auteurs distincts qui l'ont donnée
  official        official_check NOT NULL DEFAULT 'unchecked',
  checked_at      timestamptz,
  status          fact_status NOT NULL DEFAULT 'candidate',
  first_seen      timestamptz NOT NULL,
  last_seen       timestamptz NOT NULL,         -- dernière confirmation dans le chat
  document_id     bigint REFERENCES documents(id) ON DELETE SET NULL  -- tant qu'il est actif
);
CREATE INDEX qa_facts_group_idx  ON qa_facts (server_id, question_group);
CREATE INDEX qa_facts_status_idx ON qa_facts (server_id, status);
CREATE INDEX qa_facts_q_idx      ON qa_facts USING hnsw (q_embedding vector_cosine_ops);

-- Une occurrence dans le chat : tel auteur a donné (ou contredit) tel fait.
CREATE TABLE qa_claims (
  id               bigserial PRIMARY KEY,
  fact_id          bigint NOT NULL REFERENCES qa_facts(id) ON DELETE CASCADE,
  stance           smallint NOT NULL CHECK (stance IN (-1, 1)),  -- 1 soutient, -1 contredit
  question_msg_id  bigint REFERENCES chat_messages(id),
  answer_msg_ids   bigint[] NOT NULL,           -- une réponse tient souvent sur plusieurs messages
  server_id        int NOT NULL,
  author_hash      text NOT NULL,
  author_role      text NOT NULL,
  extraction_conf  real NOT NULL CHECK (extraction_conf BETWEEN 0 AND 1),  -- certitude du LLM extracteur
  seen_at          timestamptz NOT NULL,
  FOREIGN KEY (server_id, author_hash) REFERENCES players(server_id, author_hash)
);
CREATE INDEX qa_claims_fact_idx ON qa_claims (fact_id);

-- ---------------------------------------------------------------------------
-- Journal des requêtes, cache sémantique et retours
-- ---------------------------------------------------------------------------

CREATE TABLE queries (
  id           bigserial PRIMARY KEY,
  server_id    int REFERENCES servers(id),
  mc_version   int,
  player_hash  text,                            -- pour la limite de débit et les stats, pas d'identité
  question     text NOT NULL,
  q_embedding  vector(1024),
  answer       text,
  chunk_ids    bigint[],                        -- contexte donné au LLM
  recipe_ids   bigint[],
  fact_ids     bigint[],                        -- faits du chat utilisés (un 👎 les pénalise)
  model        text,
  latency_ms   int,
  feedback     smallint CHECK (feedback IN (-1, 1)),
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX queries_embedding_idx ON queries USING hnsw (q_embedding vector_cosine_ops);
CREATE INDEX queries_created_idx   ON queries (created_at);
