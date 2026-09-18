-- Record WHICH model produced each embedding.
--
-- THE FAILURE THIS PREVENTS. Local development embeds with
-- intfloat/multilingual-e5-large; production embeds with cohere.embed-english-v3.
-- Both are 1024-dimensional, so the column type accepts either and a mismatch
-- raises nothing at all. Cosine distance between two unrelated vector spaces
-- still returns a number between 0 and 1 — it is simply meaningless. Retrieval
-- returns four confident, wrong chunks and the model cites them.
--
-- It does not take a deployment to hit this. Flip LLM_PROVIDER locally and re-run
-- `make ingest`: the old ingest selected `WHERE embedding IS NULL`, so rows already
-- embedded by the previous model were skipped, and the corpus stayed in the old
-- space while queries arrived in the new one. Silent, and permanent until someone
-- truncates the table.
--
-- With this column, `unembedded_chunks` selects rows whose model is not the
-- current one, so switching providers re-embeds on the next ingest, and
-- `search_knowledge` filters to the current model, so a half-migrated corpus
-- returns fewer rows rather than wrong ones.
--
-- Nullable with no default and no backfill: every existing row has
-- `embedding IS NULL` (the seed inserts text only — embedding is the ingest
-- step's job), so there is no historical vector whose provenance we would have
-- to guess at. A DEFAULT here would be that guess, asserted as fact.
ALTER TABLE knowledge_chunks ADD COLUMN embedding_model text;

-- Partial index, because the only query that reads this column also requires
-- `embedding IS NOT NULL` — un-embedded rows are never candidates, so indexing
-- them is dead weight that has to be maintained on every ingest write.
CREATE INDEX knowledge_chunks_model_idx
    ON knowledge_chunks (embedding_model)
    WHERE embedding IS NOT NULL;
