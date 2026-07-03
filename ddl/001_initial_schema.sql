-- =============================================================================
-- ConnBot PostgreSQL Schema — MVP Initial
-- =============================================================================
-- 실행: psql -h localhost -U connbot -d connbot -f 001_initial_schema.sql
-- Docker: docker exec -i connbot-postgres psql -U connbot -d connbot < 001_initial_schema.sql

-- uuid 생성 확장
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- 1. users
-- =============================================================================
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slack_user_id   TEXT UNIQUE,
    email           TEXT,
    name            TEXT,
    profile_image_url TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_slack_user_id ON users (slack_user_id) WHERE slack_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_users_email ON users (email) WHERE email IS NOT NULL;


-- =============================================================================
-- 2. chat_sessions
-- =============================================================================
CREATE TABLE IF NOT EXISTS chat_sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    session_type    TEXT NOT NULL DEFAULT 'manual',
    slack_channel_id TEXT,
    slack_thread_ts TEXT,
    project_id      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ,
    deleted_at      TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_status_updated
    ON chat_sessions (user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_project_updated
    ON chat_sessions (user_id, project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_slack
    ON chat_sessions (slack_channel_id, slack_thread_ts);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_last_message
    ON chat_sessions (last_message_at DESC);


-- =============================================================================
-- 3. chat_messages
-- =============================================================================
CREATE TABLE IF NOT EXISTS chat_messages (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id          UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role                TEXT NOT NULL,        -- user, assistant, system, tool
    content             TEXT NOT NULL,
    model_name          TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    parent_message_id   UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_chat_messages_session_created
    ON chat_messages (session_id, created_at ASC);
CREATE INDEX IF NOT EXISTS ix_chat_messages_user_created
    ON chat_messages (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_chat_messages_role
    ON chat_messages (role);
CREATE INDEX IF NOT EXISTS ix_chat_messages_metadata
    ON chat_messages USING GIN (metadata);


-- =============================================================================
-- 4. session_summaries
-- =============================================================================
CREATE TABLE IF NOT EXISTS session_summaries (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id              UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    summary                 TEXT NOT NULL,
    decisions               JSONB NOT NULL DEFAULT '[]'::jsonb,
    open_questions          JSONB NOT NULL DEFAULT '[]'::jsonb,
    key_entities            JSONB NOT NULL DEFAULT '[]'::jsonb,
    covered_until_message_id UUID,
    covered_message_count   INTEGER NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_session_summaries_session_updated
    ON session_summaries (session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_session_summaries_user_updated
    ON session_summaries (user_id, updated_at DESC);


-- =============================================================================
-- 5. memories
-- =============================================================================
CREATE TABLE IF NOT EXISTS memories (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id          TEXT,
    scope               TEXT NOT NULL,       -- global, project, session
    memory_type         TEXT NOT NULL,       -- preference, decision, constraint, workflow, fact
    title               TEXT,
    content             TEXT NOT NULL,
    source_session_id   UUID,
    source_message_ids  UUID[],
    importance          REAL NOT NULL DEFAULT 0.5,
    confidence          REAL NOT NULL DEFAULT 0.5,
    status              TEXT NOT NULL DEFAULT 'active',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed_at    TIMESTAMPTZ,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_memories_user_project_status
    ON memories (user_id, project_id, status);
CREATE INDEX IF NOT EXISTS ix_memories_user_scope_status
    ON memories (user_id, scope, status);
CREATE INDEX IF NOT EXISTS ix_memories_type_status
    ON memories (memory_type, status);
CREATE INDEX IF NOT EXISTS ix_memories_importance
    ON memories (importance DESC);
CREATE INDEX IF NOT EXISTS ix_memories_metadata
    ON memories USING GIN (metadata);


-- =============================================================================
-- 6. improvement_events
-- =============================================================================
CREATE TABLE IF NOT EXISTS improvement_events (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id              UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
    message_id              UUID REFERENCES chat_messages(id) ON DELETE SET NULL,
    assistant_message_id    UUID REFERENCES chat_messages(id) ON DELETE SET NULL,
    event_type              TEXT NOT NULL,
    severity                TEXT NOT NULL DEFAULT 'medium',
    user_query              TEXT,
    assistant_answer        TEXT,
    reason                  TEXT,
    similar_message_ids     UUID[],
    repeated_count          INTEGER NOT NULL DEFAULT 1,
    status                  TEXT NOT NULL DEFAULT 'open',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at             TIMESTAMPTZ,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_improvement_events_user_status_created
    ON improvement_events (user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_improvement_events_session_created
    ON improvement_events (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_improvement_events_type_status
    ON improvement_events (event_type, status);
CREATE INDEX IF NOT EXISTS ix_improvement_events_severity_status
    ON improvement_events (severity, status);
CREATE INDEX IF NOT EXISTS ix_improvement_events_created
    ON improvement_events (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_improvement_events_metadata
    ON improvement_events USING GIN (metadata);


-- =============================================================================
-- updated_at 자동 갱신 트리거
-- =============================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'users', 'chat_sessions', 'chat_messages',
            'session_summaries', 'memories', 'improvement_events'
        ])
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_updated_at ON %I; '
            'CREATE TRIGGER trg_%s_updated_at '
            'BEFORE UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();',
            tbl, tbl, tbl, tbl
        );
    END LOOP;
END;
$$;
