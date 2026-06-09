"""Compare LLM tables across SQLite DB files."""
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def llm_snapshot(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    print("---", db_path.name, f"({db_path.stat().st_size} bytes)")
    cur.execute("SELECT * FROM resume_app_llmproviderconfig ORDER BY id")
    for r in cur.fetchall():
        d = dict(r)
        ek = d.get("encrypted_api_key") or ""
        print(
            f"  config id={d['id']} provider={d['provider']!r} active={d.get('is_active')} "
            f"prio={d.get('priority')} model={d.get('default_model')!r} key_len={len(ek)}"
        )
    cur.execute("SELECT * FROM resume_app_llmproviderpreference ORDER BY priority, id")
    for r in cur.fetchall():
        d = dict(r)
        print(
            f"  pref id={d['id']} cfg={d['provider_config_id']} model={d['model']!r} "
            f"prio={d['priority']} local={d['is_local']}"
        )
    conn.close()


for name in ["db.sqlite3", "backups/db-20260525-122804.sqlite3", "db.sqlite3.corrupt"]:
    p = BASE / name
    if p.exists():
        llm_snapshot(p)
    print()
