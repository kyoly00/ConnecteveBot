-- ConnBot: 회의실 예약 챗봇 리마인더 + Slack 채널
-- 적용: docker exec -i connbot-postgres psql -U connbot -d connbot < ddl/003_room_booking_reminders.sql

ALTER TABLE room_bookings
    ADD COLUMN IF NOT EXISTS slack_channel_id TEXT,
    ADD COLUMN IF NOT EXISTS reminder_minutes_before INTEGER,
    ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_room_bookings_reminder_pending
    ON room_bookings (status, reminder_minutes_before, reminder_sent_at)
    WHERE status = 'active' AND reminder_minutes_before IS NOT NULL AND reminder_sent_at IS NULL;
