from pace.config import load
from pace.db import DB


def test_migrate_is_idempotent(tmp_path):
    db = DB(tmp_path / "pace.db")
    db.migrate()
    assert db.one("PRAGMA user_version")[0] == 2
    assert {r["name"] for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")} >= {
        "projects", "items", "plans", "qa_rounds", "runs", "proposals", "events", "kv", "groups"}


def test_tx_rolls_back(tmp_path):
    db = DB(tmp_path / "pace.db")
    try:
        with db.tx():
            db.kv_set("a", 1)
            raise RuntimeError
    except RuntimeError:
        pass
    assert db.kv_get("a") is None
    db.kv_set("a", {"x": 1})
    assert db.kv_get("a") == {"x": 1}


def test_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('data_dir = "/tmp/x"\nport = 9000\n')
    c = load(p)
    assert c.port == 9000 and str(c.db_path) == "/tmp/x/pace.db"
