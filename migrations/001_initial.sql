-- Initial schema. Every statement is idempotent so a re-run is harmless.

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    name                TEXT,
    guild_id            TEXT NOT NULL,
    channel_id          TEXT NOT NULL,
    channel_name        TEXT,
    text_channel_id     TEXT,
    started_by_user_id  TEXT,
    start_time          TEXT NOT NULL,
    end_time            TEXT,
    completed           INTEGER NOT NULL DEFAULT 0,
    transcribed         INTEGER NOT NULL DEFAULT 0,
    cancelled           INTEGER NOT NULL DEFAULT 0,
    audio_expires_at    TEXT,
    participants_json   TEXT NOT NULL DEFAULT '{}',
    offsets_json        TEXT NOT NULL DEFAULT '{}',
    language            TEXT,
    model_used          TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions (guild_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions (completed, cancelled);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions (audio_expires_at);

CREATE TABLE IF NOT EXISTS characters (
    user_id         TEXT PRIMARY KEY,
    character_name  TEXT NOT NULL,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS transcription_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions (id),
    queued_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON transcription_queue (status, queued_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_session_open
    ON transcription_queue (session_id)
    WHERE status IN ('pending', 'processing');
