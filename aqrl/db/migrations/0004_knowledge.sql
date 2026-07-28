-- Knowledge (Backend-Schema §9-10).
--
-- Two bodies of knowledge that must never be conflated (TRD §12.3):
--
--   knowledge_entries   internal, TESTED     — written by A5 from experiments
--   external_knowledge  external, UNTESTED   — the Librarian's reading of papers
--
-- An external row is a *candidate worth testing*. It earns the weight of
-- "confirmed" only when an experiment tests it and A5 writes the result into
-- knowledge_entries. `extraction_confidence` measures reading accuracy, never
-- truth, and no promotion decision may cite it as evidence.

CREATE TABLE knowledge_entries (
    id                     {{PK}},
    uid                    TEXT NOT NULL UNIQUE,
    -- Nullable: cross-experiment lessons have neither.
    experiment_id          INTEGER REFERENCES experiments(id),
    strategy_id            INTEGER REFERENCES strategies(id),
    entry_type             TEXT NOT NULL CHECK (entry_type IN (
                               'experiment_record', 'lesson', 'global_rule', 'pattern')),
    scope                  TEXT NOT NULL CHECK (scope IN ('experiment', 'family', 'market', 'global')),
    title                  TEXT NOT NULL,
    statement              TEXT NOT NULL,
    evidence               {{JSON}},
    evidence_count         INTEGER NOT NULL DEFAULT 0,
    -- Honest bookkeeping — a lesson with contradictions must look less certain.
    counter_evidence_count INTEGER NOT NULL DEFAULT 0,
    confidence             REAL,
    applicable_markets     {{JSON}},
    applicable_timeframes  {{JSON}},
    applicable_regimes     {{JSON}},
    -- MANDATORY — this is what self-propels the lab.
    future_ideas           {{JSON}},
    embedding_id           TEXT,
    -- Knowledge is REVISED, NEVER DELETED. The lab's memory only grows, and you
    -- can always see what it used to believe.
    superseded_by          INTEGER REFERENCES knowledge_entries(id),
    created_at             TEXT NOT NULL
);

-- The knowledge graph (TRD §12.5). An edge with no experiment backing must not
-- exist. Repeated observation updates counts, never duplicates rows.
CREATE TABLE knowledge_edges (
    id                     {{PK}},
    subject                TEXT NOT NULL,
    predicate              TEXT NOT NULL CHECK (predicate IN (
                               'works_in', 'fails_in', 'pairs_well_with', 'pairs_poorly_with',
                               'requires', 'degrades_with')),
    object                 TEXT NOT NULL,
    confidence             REAL,
    evidence_count         INTEGER NOT NULL DEFAULT 0,
    counter_evidence_count INTEGER NOT NULL DEFAULT 0,
    supporting_experiments {{JSON}},
    first_observed_at      TEXT,
    last_updated_at        TEXT,
    UNIQUE (subject, predicate, object)
);

-- The human-readable record per strategy (TRD §16).
CREATE TABLE lab_notebooks (
    id                {{PK}},
    uid               TEXT NOT NULL UNIQUE,
    experiment_id     INTEGER REFERENCES experiments(id),
    strategy_id       INTEGER REFERENCES strategies(id),
    hypothesis        TEXT,
    result            TEXT,
    reason            TEXT,
    evidence          TEXT,
    confidence        REAL,
    -- MANDATORY, NON-EMPTY. Enforced by the repository layer, since a CHECK
    -- cannot see inside a JSON document portably.
    next_questions    {{JSON}} NOT NULL,
    rendered_markdown TEXT,
    created_at        TEXT NOT NULL
);

-- Raw ingested artifacts. Read once, ever.
CREATE TABLE external_documents (
    id                {{PK}},
    uid               TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL CHECK (source IN ('arxiv', 'ssrn', 'github', 'blog', 'journal', 'book')),
    source_id         TEXT,
    url               TEXT,
    title             TEXT,
    authors           TEXT,
    published_at      TEXT,
    content_hash      TEXT NOT NULL UNIQUE,      -- deduplication
    raw_path          TEXT,
    ingested_at       TEXT NOT NULL,
    extracted_at      TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending'
                          CHECK (extraction_status IN ('pending', 'chunked', 'done', 'failed', 'irrelevant')),
    -- Cheap filter BEFORE spending LLM tokens.
    relevance_score   REAL,
    chunk_count       INTEGER
);

-- One row per piece a large document was split into, so a synthesized idea
-- points at an EXACT PASSAGE rather than "somewhere in this paper".
CREATE TABLE document_chunks (
    id                {{PK}},
    uid               TEXT NOT NULL UNIQUE,
    document_id       INTEGER NOT NULL REFERENCES external_documents(id),
    chunk_index       INTEGER NOT NULL,
    -- Populated when the source has structural headings — chunking is by
    -- structure, not a blind token window.
    section_title     TEXT,
    char_start        INTEGER,
    char_end          INTEGER,
    -- Pass-1 output: candidate claims in THIS CHUNK ALONE, before synthesis.
    chunk_extraction  {{JSON}},
    processed_at      TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (document_id, chunk_index)
);

-- ONE ROW IS ONE IDEA, never one row per document. A single paper's synthesis
-- pass typically yields 2-3 of these.
CREATE TABLE external_knowledge (
    id                        {{PK}},
    uid                       TEXT NOT NULL UNIQUE,
    document_id               INTEGER NOT NULL REFERENCES external_documents(id),
    -- Which chunks this idea was synthesized from — the traceability link.
    source_chunk_ids          {{JSON}},
    layer                     TEXT CHECK (layer IN ('research', 'market', 'software', 'infrastructure')),
    -- ONE CLEAR SENTENCE. If it needs a paragraph, synthesis didn't finish.
    core_idea                 TEXT NOT NULL,
    category                  TEXT,
    applicable_markets        {{JSON}},
    applicable_timeframes     {{JSON}},
    strengths                 TEXT,
    weaknesses                TEXT,
    implementation_difficulty TEXT CHECK (implementation_difficulty IN ('low', 'medium', 'high')),
    required_operators        {{JSON}},
    -- Directly consumable by A1 — this is what makes the record actionable.
    proposed_experiments      {{JSON}},
    -- Above a threshold, triggers a GENERATE_SPEC job immediately.
    novelty_score             REAL,
    -- The Librarian's confidence that it READ THE SOURCE CORRECTLY. Explicitly
    -- NOT a claim the idea is true.
    extraction_confidence     REAL,
    -- Always 'external_claim'. Contrasts with knowledge_entries (internal,
    -- tested) — never conflate.
    evidence_tier             TEXT NOT NULL DEFAULT 'external_claim'
                                  CHECK (evidence_tier IN ('external_claim')),
    extracted_by              TEXT,
    extraction_prompt_version TEXT,
    extracted_at              TEXT,
    related_knowledge_ids     {{JSON}},
    embedding_id              TEXT,
    used_in_specs             {{JSON}},
    created_at                TEXT NOT NULL
);

-- The curiosity queue (TRD §12.4).
CREATE TABLE research_questions (
    id                    {{PK}},
    uid                   TEXT NOT NULL UNIQUE,
    question              TEXT NOT NULL,
    motivation            TEXT,
    origin_type           TEXT CHECK (origin_type IN (
                              'experiment_failure', 'pattern_detection', 'human', 'live_degradation')),
    origin_experiment_id  INTEGER REFERENCES experiments(id),
    origin_knowledge_id   INTEGER REFERENCES knowledge_entries(id),
    priority              INTEGER NOT NULL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open', 'searching', 'answered', 'abandoned')),
    search_terms          {{JSON}},
    answer_knowledge_ids  {{JSON}},
    -- DID ASKING THIS EVER PAY OFF? Loop-closure tracking (TRD §12.4).
    produced_spec_ids     {{JSON}},
    created_at            TEXT NOT NULL,
    resolved_at           TEXT
);
