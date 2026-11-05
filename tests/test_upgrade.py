"""A library made by an earlier version opens in this one."""
from sqlalchemy import inspect, text

from litreview_local import db


def test_columns_added_since_are_added_to_an_old_library(clean_db):
    added = [(table, column) for table, column, _ in db._MIGRATION_COLUMNS]
    assert added, "nothing to test"
    with db.engine.begin() as conn:
        for table, column in added:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    missing = {c["name"] for c in inspect(db.engine).get_columns("papers")}
    assert "volume" not in missing
    db.run_migrations()
    db.run_migrations()  # and again: nothing to do the second time
    present = {(t, c["name"]) for t in {t for t, _ in added}
               for c in inspect(db.engine).get_columns(t)}
    assert set(added) <= present
