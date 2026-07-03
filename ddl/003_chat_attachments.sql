-- Slack/채팅 첨부 메타 (바이너리는 Data/chat_attachments)
CREATE TABLE IF NOT EXISTS chat_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
    slack_thread_ts TEXT,
    user_text TEXT,
    attachment_path TEXT NOT NULL,
    attachment_title TEXT NOT NULL,
    attachment_summary TEXT,
    attachment_kind TEXT,
    slack_file_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_chat_attachments_user_created
    ON chat_attachments (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_chat_attachments_session_created
    ON chat_attachments (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_chat_attachments_slack_thread
    ON chat_attachments (slack_thread_ts);
