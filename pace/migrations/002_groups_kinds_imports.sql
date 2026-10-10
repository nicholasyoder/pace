-- Roadmap format v2: preamble, groups (tiers/releases/milestones), kind tags,
-- and import items (an item whose plan is a roadmap, applied on approval).
ALTER TABLE projects ADD COLUMN preamble TEXT NOT NULL DEFAULT '';

CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, title)
);

ALTER TABLE items ADD COLUMN group_id INTEGER REFERENCES groups(id);
ALTER TABLE items ADD COLUMN kind TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN type TEXT NOT NULL DEFAULT 'item' CHECK (type IN ('item', 'import'));
