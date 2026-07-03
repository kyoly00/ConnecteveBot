-- ConnBot: Outlook 회의실 예약 event_id 저장
-- 적용: docker exec -i connbot-postgres psql -U connbot -d connbot < ddl/002_room_bookings.sql

CREATE TABLE IF NOT EXISTS room_bookings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    slack_user_id   TEXT,
    room_name       TEXT NOT NULL,
    room_display    TEXT NOT NULL,
    subject         TEXT NOT NULL,
    event_subject   TEXT NOT NULL,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    organizer_email TEXT NOT NULL,
    outlook_event_id TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'active',
    room_confirmed  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cancelled_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_room_bookings_user_status_created
    ON room_bookings (user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_room_bookings_slack_status_created
    ON room_bookings (slack_user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_room_bookings_outlook_event_id
    ON room_bookings (outlook_event_id);

CREATE INDEX IF NOT EXISTS ix_room_bookings_room_start
    ON room_bookings (room_name, start_time);
