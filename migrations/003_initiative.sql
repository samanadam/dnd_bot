-- Initiative totals players report from Discord for the DM's tracker.
-- One pending total per player and name: reporting again replaces it.

CREATE TABLE IF NOT EXISTS initiative_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    label       TEXT NOT NULL,
    value       INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, label)
);
