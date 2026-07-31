-- Stage 7 (Implementation_Plan §10): the Research Brief's relevance search.
--
-- One row per embedded knowledge-base entry, cached so a `knowledge_entries`
-- or `external_knowledge` row is embedded once, ever — not once per brief
-- assembly. `owner_table`/`owner_id` is a plain polymorphic reference rather
-- than two nullable FK columns: SQLite cannot express "exactly one of these
-- FKs is set" as a constraint, and the CHECK below is the portable substitute
-- (works unchanged after the eventual PostgreSQL swap, TRD §3.2).
--
-- `vector` is `{{JSON}}`, not a vector extension type: the relevance search
-- this backs is a SQL-bounded candidate set (a few hundred rows) ranked by
-- cosine similarity in Python, not a whole-table ANN index — TRD §21 marks a
-- dedicated vector database as a Stage 13 scale-out concern, not a v1 one.

CREATE TABLE embeddings (
    id          {{PK}},
    uid         TEXT NOT NULL UNIQUE,
    owner_table TEXT NOT NULL CHECK (owner_table IN ('knowledge_entries', 'external_knowledge')),
    owner_id    INTEGER NOT NULL,
    -- The embedding model's identity — never hand-bumped, same reasoning as
    -- `operator_library_version` (aqrl/operators/registry.py). A stored
    -- vector is only comparable to a query vector from the same model.
    model       TEXT NOT NULL,
    vector      {{JSON}} NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (owner_table, owner_id)
);

CREATE INDEX idx_embeddings_owner ON embeddings (owner_table, owner_id);
