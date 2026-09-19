-- Campaigns: several games sharing one Discord server.

CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
    channel_id  TEXT UNIQUE,
    language    TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Names Whisper should expect. Order is kept: the first ones survive the
-- prompt length cap.
CREATE TABLE IF NOT EXISTS campaign_terms (
    campaign_id TEXT NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    term        TEXT NOT NULL COLLATE NOCASE,
    position    INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, term)
);

-- "heard" is what Whisper writes, "correct" is what it should have written.
CREATE TABLE IF NOT EXISTS campaign_corrections (
    campaign_id TEXT NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    heard       TEXT NOT NULL COLLATE NOCASE,
    correct     TEXT NOT NULL,
    PRIMARY KEY (campaign_id, heard)
);

-- One player can be a different character in each campaign.
CREATE TABLE IF NOT EXISTS campaign_characters (
    campaign_id    TEXT NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    user_id        TEXT NOT NULL,
    character_name TEXT NOT NULL,
    updated_at     TEXT,
    PRIMARY KEY (campaign_id, user_id)
);

-- NULL campaign_id means unassigned. relabel_json maps user_id -> the label a
-- transcript should show once the session has been assigned; empty means "as
-- recorded". base_labels_json holds each speaker's label without any character
-- name (nickname or username), so re-tagging a session can never carry one
-- campaign's character name into another.
ALTER TABLE sessions ADD COLUMN campaign_id TEXT REFERENCES campaigns (id);
ALTER TABLE sessions ADD COLUMN relabel_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN base_labels_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_sessions_campaign ON sessions (campaign_id);
