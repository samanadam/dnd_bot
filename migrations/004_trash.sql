-- A deleted session waits in the trash before it is really removed, so a
-- mistaken click can be undone. NULL means the session is not in the trash.

ALTER TABLE sessions ADD COLUMN deleted_at TEXT;
CREATE INDEX IF NOT EXISTS sessions_deleted ON sessions (deleted_at);
