-- Who is fighting in the battle the portal is showing to players, and the
-- initiative bonus on their sheet. `/init` without a total rolls with it. The
-- portal replaces the whole roster when a battle is shown and clears it when the
-- battle ends; nothing else writes here.

CREATE TABLE IF NOT EXISTS initiative_roster (
    user_id     TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    bonus       INTEGER NOT NULL,
    campaign_id TEXT,
    updated_at  TEXT NOT NULL
);

-- How a report came in: 'typed' (the player gave a total) or 'rolled' (the bot
-- rolled it from the roster).
ALTER TABLE initiative_reports ADD COLUMN source TEXT NOT NULL DEFAULT 'typed';
