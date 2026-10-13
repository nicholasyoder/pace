-- Proposed items in the inbox (§6): when they were proposed and decided, and
-- the roadmap item an accepted one became.
ALTER TABLE proposals ADD COLUMN created_at TEXT;
ALTER TABLE proposals ADD COLUMN decided_at TEXT;
ALTER TABLE proposals ADD COLUMN item_id TEXT REFERENCES items(id);
