-- managed_room_events: 회의실 Outlook 캘린더 projection (read model)
CREATE TABLE IF NOT EXISTS managed_room_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    room_email TEXT NOT NULL,
    room_name TEXT NOT NULL,
    room_display TEXT NOT NULL,
    subject TEXT NOT NULL,
    event_subject TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    organizer_email TEXT NOT NULL,
    organizer_name TEXT,
    outlook_event_id TEXT NOT NULL,
    organizer_event_id TEXT,
    attendee_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'active',
    bot_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    bot_slack_user_id TEXT,
    slack_channel_id TEXT,
    reminder_minutes_before INTEGER,
    reminder_sent_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_managed_room_events_room_outlook UNIQUE (room_email, outlook_event_id)
);

CREATE INDEX IF NOT EXISTS ix_managed_room_events_room_start
    ON managed_room_events (room_email, start_time);
CREATE INDEX IF NOT EXISTS ix_managed_room_events_organizer_start
    ON managed_room_events (organizer_email, start_time);
CREATE INDEX IF NOT EXISTS ix_managed_room_events_status_start
    ON managed_room_events (status, start_time);
CREATE INDEX IF NOT EXISTS ix_managed_room_events_organizer_event_id
    ON managed_room_events (organizer_event_id)
    WHERE organizer_event_id IS NOT NULL;
