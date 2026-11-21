import json
import sqlite3
from contextlib import closing, contextmanager

from app.errors import FieldError
from app.manifest import canonical


class ArchiveStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            yield connection

    def setup(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS shift_archives (
                    id TEXT PRIMARY KEY,
                    manifest TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    input TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS lineage_ledger (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    segment TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    attribution TEXT NOT NULL,
                    prior_run TEXT,
                    created TEXT NOT NULL,
                    superseded_by TEXT,
                    UNIQUE(run_id, segment)
                )
            """)

    def insert(self, manifest, evidence, source):
        with self.connect() as connection:
            cursor = connection.execute("INSERT INTO shift_archives VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                                        (manifest["id"], canonical(manifest), canonical(evidence), canonical(source)))
            record = connection.execute("SELECT manifest FROM shift_archives WHERE id = ?", (manifest["id"],)).fetchone()
            return json.loads(record[0]), cursor.rowcount == 1

    def insert_run_with_lineage(self, manifest, evidence, source, lineage, created):
        """归卷与归因账在同一事务提交：任一笔失败整体回滚，旧版完整保留。"""
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO shift_archives VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (manifest["id"], canonical(manifest), canonical(evidence), canonical(source)))
            created_run = cursor.rowcount == 1
            if created_run:
                for item in lineage:
                    prior_run = next((link.get("run") for link in item["links"] if link.get("run")), None)
                    connection.execute(
                        "INSERT INTO lineage_ledger (run_id, segment, direction, attribution, prior_run, created)"
                        " VALUES (?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(run_id, segment) DO NOTHING",
                        (manifest["id"], item["segment"], item["direction"], canonical(item), prior_run, created))
            record = connection.execute("SELECT manifest FROM shift_archives WHERE id = ?",
                                        (manifest["id"],)).fetchone()
        return json.loads(record[0]), created_run

    def append_lineage(self, run_id, lineage, created):
        """追加归因账：同一 run 的同段只允许一条，旧结论不被覆盖，失败整体回滚。"""
        with self.connect() as connection:
            for item in lineage:
                prior_run = next((link.get("run") for link in item["links"] if link.get("run")), None)
                connection.execute(
                    "INSERT INTO lineage_ledger (run_id, segment, direction, attribution, prior_run, created)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(run_id, segment) DO NOTHING",
                    (run_id, item["segment"], item["direction"], canonical(item), prior_run, created))

    def lineage_ledger(self, run_id):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT attribution FROM lineage_ledger WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def fetch_by_shift(self, shift):
        with self.connect() as connection:
            record = connection.execute(
                "SELECT manifest, evidence, input FROM shift_archives WHERE json_extract(manifest, '$.shift') = ?"
                " ORDER BY rowid DESC LIMIT 1", (shift,)).fetchone()
        if record is None:
            return None
        return tuple(json.loads(value) for value in record)

    def fetch(self, run_id):
        with self.connect() as connection:
            record = connection.execute("SELECT manifest, evidence, input FROM shift_archives WHERE id = ?", (run_id,)).fetchone()
        if record is None:
            raise FieldError("UNKNOWN_RUN", "该归卷未登记", 404)
        return tuple(json.loads(value) for value in record)

    def history(self):
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT manifest FROM shift_archives ORDER BY rowid DESC")]
