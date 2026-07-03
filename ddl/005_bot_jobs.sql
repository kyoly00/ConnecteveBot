-- bot_jobs: Slack / Graph / Confluence 웹훅 비동기 작업 큐
CREATE TABLE IF NOT EXISTS bot_jobs (
    id BIGSERIAL PRIMARY KEY,

    source TEXT NOT NULL,              -- slack, graph, confluence
    source_event_id TEXT NOT NULL,     -- Slack event_id / Graph notification id
    event_type TEXT NOT NULL,

    team_id TEXT,
    channel_id TEXT,
    user_id TEXT,
    thread_ts TEXT,
    event_ts TEXT,

    conversation_key TEXT NOT NULL,

    payload JSONB NOT NULL,

    status TEXT NOT NULL DEFAULT 'queued',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,

    locked_by TEXT,
    locked_at TIMESTAMPTZ,

    next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    error_code TEXT,
    error_message TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_bot_jobs_source_event UNIQUE (source, source_event_id)
);

CREATE INDEX IF NOT EXISTS ix_bot_jobs_status_next_run
    ON bot_jobs (status, next_run_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS ix_bot_jobs_processing_locked_at
    ON bot_jobs (locked_at)
    WHERE status = 'processing';

CREATE INDEX IF NOT EXISTS ix_bot_jobs_conversation_key
    ON bot_jobs (conversation_key, created_at DESC);
