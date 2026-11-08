-- Defense in depth for accidental direct writes. Application constructors still
-- enforce the full domain contract. This is not a database authorization boundary.
CREATE FUNCTION valid_scope(document JSONB) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE STRICT AS $$
    SELECT COALESCE(
        jsonb_typeof(document) = 'object'
        AND jsonb_typeof(document->'environment_id') = 'string'
        AND length(document->>'environment_id') <= 63
        AND document->>'environment_id' ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
        AND jsonb_typeof(document->'generation') = 'number'
        AND document->>'generation' ~ '^[1-9][0-9]*$'
        AND (document->>'generation')::numeric <= 9223372036854775807
        AND document->>'environment' IN ('development', 'staging', 'production')
        AND document->>'revision' ~ '^[0-9a-f]{40}$'
        AND jsonb_typeof(document->'service') = 'string'
        AND length(btrim(document->>'service')) > 0,
        FALSE
    )
$$;
ALTER TABLE evidence_objects ADD CONSTRAINT evidence_scope_v2 CHECK (COALESCE(
    metadata::jsonb->'schema_version' = '2'::jsonb
    AND valid_scope(metadata::jsonb->'evidence'->'scope'), FALSE));
ALTER TABLE findings ADD CONSTRAINT finding_scope_v2 CHECK (COALESCE(
    payload::jsonb->'schema_version' = '2'::jsonb
    AND valid_scope(payload::jsonb->'scope')
    AND payload::jsonb->'scope' = payload::jsonb->'evidence'->'scope', FALSE));
ALTER TABLE previews ADD CONSTRAINT preview_scope_v2 CHECK (COALESCE(
    payload::jsonb->'schema_version' = '2'::jsonb
    AND valid_scope(payload::jsonb->'scope')
    AND payload::jsonb->'scope'->>'environment' <> 'production'
    AND payload::jsonb->'scope'->>'environment_id' = environment_id
    AND jsonb_typeof(payload::jsonb->'repository_id') = 'number'
    AND jsonb_typeof(payload::jsonb->'pr_number') = 'number'
    AND payload::jsonb->>'repository_id' ~ '^[1-9][0-9]*$'
    AND payload::jsonb->>'pr_number' ~ '^[1-9][0-9]*$'
    AND (payload::jsonb->>'repository_id')::numeric <= 9223372036854775807
    AND (payload::jsonb->>'pr_number')::numeric <= 9223372036854775807
    AND environment_id = 'preview-r' || (payload::jsonb->>'repository_id')
        || '-pr' || (payload::jsonb->>'pr_number'), FALSE));
ALTER TABLE checkpoints ADD CONSTRAINT checkpoint_scope_v2 CHECK (COALESCE(
    scope::jsonb->'schema_version' = '2'::jsonb
    AND valid_scope(scope::jsonb->'scope'), FALSE));
CREATE INDEX findings_state_id ON findings(state, id);
CREATE INDEX preview_events_identity ON preview_events(environment_id, generation, sequence);
