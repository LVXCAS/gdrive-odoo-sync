# -*- coding: utf-8 -*-
"""SQLite datastore for DriftWatch.

This is the file every other module builds against, so its API is the contract
and is documented as such.

WHY SQLite rather than Postgres: this service is single-node and its write
pattern is one crawl at a time. SQLite removes an entire operational surface
(a server to run, back up, patch and authenticate to) in exchange for a
limitation -- one writer -- that this workload never hits. Moving to Postgres
later is a change of connection string and placeholder style, not of schema.

WHY every hash is stored as hex text rather than BLOB: it is compared by
equality, printed in reports and diffed by humans. Hex costs 2x the bytes of
the raw digest, on data that is already tiny, and saves a conversion at every
single read site.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per Drive object, folders included.
CREATE TABLE IF NOT EXISTS node (
    file_id        TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    mime_type      TEXT NOT NULL,
    parent_id      TEXT,
    drive_version  TEXT,
    modified_time  TEXT,
    size           INTEGER,
    owner_email    TEXT,
    is_folder      INTEGER NOT NULL DEFAULT 0,
    trashed        INTEGER NOT NULL DEFAULT 0,
    shared_drive   TEXT,
    web_link       TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    -- Set when a node stops appearing in a complete crawl. Never deleted
    -- outright: disappearance is a fact worth keeping, and a node that comes
    -- back should not look brand new.
    missing_since  TEXT
);
CREATE INDEX IF NOT EXISTS ix_node_mime   ON node(mime_type);
CREATE INDEX IF NOT EXISTS ix_node_parent ON node(parent_id);
CREATE INDEX IF NOT EXISTS ix_node_name   ON node(name);

-- One row per spreadsheet TAB. A workbook with 17 tabs is 17 datasets,
-- because a tab is the unit that has a stable header and comparable rows.
CREATE TABLE IF NOT EXISTS dataset (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id        TEXT NOT NULL REFERENCES node(file_id) ON DELETE CASCADE,
    tab_id         TEXT NOT NULL,
    tab_title      TEXT NOT NULL,
    header_json    TEXT NOT NULL DEFAULT '[]',
    row_count      INTEGER NOT NULL DEFAULT 0,
    col_count      INTEGER NOT NULL DEFAULT 0,
    spec_version   TEXT,
    h_dataset      TEXT,
    bucket_hashes  TEXT,
    read_complete  INTEGER NOT NULL DEFAULT 1,
    blocked_reason TEXT,
    updated_at     TEXT NOT NULL,
    UNIQUE(file_id, tab_id)
);
CREATE INDEX IF NOT EXISTS ix_dataset_file ON dataset(file_id);

CREATE TABLE IF NOT EXISTS staged_row (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id   INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    row_number   INTEGER NOT NULL,
    a1_ref       TEXT,
    natural_key  TEXT,
    canon_json   TEXT NOT NULL DEFAULT '{}',
    raw_json     TEXT NOT NULL DEFAULT '{}',
    h_row        TEXT
);
CREATE INDEX IF NOT EXISTS ix_row_dataset ON staged_row(dataset_id);
CREATE INDEX IF NOT EXISTS ix_row_key     ON staged_row(dataset_id, natural_key);

CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    stats_json  TEXT NOT NULL DEFAULT '{}',
    error       TEXT
);

-- A finding. Never an instruction: nothing in this table is applied
-- automatically to anything.
CREATE TABLE IF NOT EXISTS drift (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES run(id) ON DELETE SET NULL,
    dataset_id  INTEGER REFERENCES dataset(id) ON DELETE CASCADE,
    drift_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'warning',
    row_ref     TEXT,
    column_key  TEXT,
    sheet_value TEXT,
    odoo_value  TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_drift_run  ON drift(run_id);
CREATE INDEX IF NOT EXISTS ix_drift_type ON drift(drift_type);

-- Drive's incremental change cursor. Per subject, because a token from one
-- corpus replayed against another silently yields a wrong delta.
CREATE TABLE IF NOT EXISTS cursor (
    subject     TEXT PRIMARY KEY,
    page_token  TEXT,
    updated_at  TEXT NOT NULL
);
"""


class Store:
    """All persistence for DriftWatch. Thread-confined; make one per process."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.set_meta('schema_version', str(SCHEMA_VERSION))

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A transaction. Rolls back on any exception."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            'INSERT INTO meta(key, value) VALUES(?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
        self.conn.commit()

    def get_meta(self, key: str, default: str = '') -> str:
        row = self.conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

    # ------------------------------------------------------------------ #
    # nodes
    # ------------------------------------------------------------------ #
    def upsert_node(self, node: dict, now: str) -> None:
        """Insert or refresh one Drive object.

        ``first_seen`` is preserved across updates; ``missing_since`` is
        cleared, because seeing a node is proof it is not missing.
        """
        self.conn.execute(
            """
            INSERT INTO node (file_id, name, mime_type, parent_id, drive_version,
                              modified_time, size, owner_email, is_folder, trashed,
                              shared_drive, web_link, first_seen, last_seen, missing_since)
            VALUES (:file_id, :name, :mime_type, :parent_id, :drive_version,
                    :modified_time, :size, :owner_email, :is_folder, :trashed,
                    :shared_drive, :web_link, :now, :now, NULL)
            ON CONFLICT(file_id) DO UPDATE SET
                name          = excluded.name,
                mime_type     = excluded.mime_type,
                parent_id     = excluded.parent_id,
                drive_version = excluded.drive_version,
                modified_time = excluded.modified_time,
                size          = excluded.size,
                owner_email   = excluded.owner_email,
                is_folder     = excluded.is_folder,
                trashed       = excluded.trashed,
                shared_drive  = excluded.shared_drive,
                web_link      = excluded.web_link,
                last_seen     = excluded.last_seen,
                missing_since = NULL
            """,
            {'now': now, **{k: node.get(k) for k in (
                'file_id', 'name', 'mime_type', 'parent_id', 'drive_version',
                'modified_time', 'size', 'owner_email', 'is_folder', 'trashed',
                'shared_drive', 'web_link')}},
        )

    def mark_missing(self, seen_ids: Iterable[str], now: str) -> int:
        """Flag nodes absent from a **complete** crawl. Never call after a
        partial read -- that is how a failed page turns into a false deletion.
        """
        ids = list(seen_ids)
        placeholders = ','.join('?' * len(ids)) or "''"
        cur = self.conn.execute(
            f'UPDATE node SET missing_since = ? '
            f'WHERE missing_since IS NULL AND file_id NOT IN ({placeholders})',
            [now, *ids])
        return cur.rowcount

    def nodes(self, mime_type: Optional[str] = None,
              include_missing: bool = False) -> list[sqlite3.Row]:
        sql = 'SELECT * FROM node WHERE trashed = 0'
        args: list = []
        if mime_type:
            sql += ' AND mime_type = ?'
            args.append(mime_type)
        if not include_missing:
            sql += ' AND missing_since IS NULL'
        return self.conn.execute(sql + ' ORDER BY name', args).fetchall()

    def count_nodes(self) -> dict:
        rows = self.conn.execute(
            'SELECT mime_type, COUNT(*) n FROM node WHERE trashed = 0 '
            'AND missing_since IS NULL GROUP BY mime_type ORDER BY n DESC').fetchall()
        return {r['mime_type']: r['n'] for r in rows}

    # ------------------------------------------------------------------ #
    # datasets and rows
    # ------------------------------------------------------------------ #
    def upsert_dataset(self, file_id: str, tab_id: str, tab_title: str,
                       header: list, now: str, **kw) -> int:
        """Create or update one tab. Returns the dataset id."""
        self.conn.execute(
            """
            INSERT INTO dataset (file_id, tab_id, tab_title, header_json, row_count,
                                 col_count, spec_version, h_dataset, bucket_hashes,
                                 read_complete, blocked_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id, tab_id) DO UPDATE SET
                tab_title      = excluded.tab_title,
                header_json    = excluded.header_json,
                row_count      = excluded.row_count,
                col_count      = excluded.col_count,
                spec_version   = excluded.spec_version,
                h_dataset      = excluded.h_dataset,
                bucket_hashes  = excluded.bucket_hashes,
                read_complete  = excluded.read_complete,
                blocked_reason = excluded.blocked_reason,
                updated_at     = excluded.updated_at
            """,
            (file_id, tab_id, tab_title, json.dumps(header, ensure_ascii=False),
             kw.get('row_count', 0), kw.get('col_count', 0), kw.get('spec_version'),
             kw.get('h_dataset'), json.dumps(kw.get('bucket_hashes') or []),
             1 if kw.get('read_complete', True) else 0,
             kw.get('blocked_reason'), now))
        row = self.conn.execute(
            'SELECT id FROM dataset WHERE file_id = ? AND tab_id = ?',
            (file_id, tab_id)).fetchone()
        return int(row['id'])

    def replace_rows(self, dataset_id: int, rows: Iterable[dict]) -> int:
        """Swap a dataset's rows wholesale, inside one transaction.

        Wholesale replacement rather than diffing is deliberate: the sheet is
        the authority for its own content, and a partial update would leave
        rows from two different reads in the same table with no way to tell
        which read produced which row.
        """
        payload = [
            (dataset_id, r.get('row_number'), r.get('a1_ref'), r.get('natural_key'),
             json.dumps(r.get('canon') or {}, ensure_ascii=False, sort_keys=True),
             json.dumps(r.get('raw') or {}, ensure_ascii=False),
             r.get('h_row'))
            for r in rows
        ]
        with self.tx() as conn:
            conn.execute('DELETE FROM staged_row WHERE dataset_id = ?', (dataset_id,))
            conn.executemany(
                'INSERT INTO staged_row (dataset_id, row_number, a1_ref, natural_key,'
                ' canon_json, raw_json, h_row) VALUES (?, ?, ?, ?, ?, ?, ?)', payload)
        return len(payload)

    def datasets(self, file_id: Optional[str] = None) -> list[sqlite3.Row]:
        sql = ('SELECT d.*, n.name AS file_name FROM dataset d '
               'JOIN node n ON n.file_id = d.file_id')
        args: list = []
        if file_id:
            sql += ' WHERE d.file_id = ?'
            args.append(file_id)
        return self.conn.execute(sql + ' ORDER BY n.name, d.tab_title', args).fetchall()

    def rows(self, dataset_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            'SELECT * FROM staged_row WHERE dataset_id = ? ORDER BY row_number',
            (dataset_id,)).fetchall()

    # ------------------------------------------------------------------ #
    # runs and drift
    # ------------------------------------------------------------------ #
    def start_run(self, kind: str, now: str) -> int:
        cur = self.conn.execute(
            'INSERT INTO run (kind, started_at) VALUES (?, ?)', (kind, now))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: str, status: str,
                   stats: Optional[dict] = None, error: str = '') -> None:
        self.conn.execute(
            'UPDATE run SET finished_at = ?, status = ?, stats_json = ?, error = ? '
            'WHERE id = ?',
            (now, status, json.dumps(stats or {}, ensure_ascii=False), error, run_id))
        self.conn.commit()

    def record_drift(self, run_id: Optional[int], dataset_id: Optional[int],
                     drift_type: str, now: str, **kw) -> None:
        self.conn.execute(
            'INSERT INTO drift (run_id, dataset_id, drift_type, severity, row_ref,'
            ' column_key, sheet_value, odoo_value, detail, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (run_id, dataset_id, drift_type, kw.get('severity', 'warning'),
             kw.get('row_ref'), kw.get('column_key'),
             _clip(kw.get('sheet_value')), _clip(kw.get('odoo_value')),
             kw.get('detail'), now))

    def drifts(self, run_id: Optional[int] = None,
               drift_type: Optional[str] = None) -> list[sqlite3.Row]:
        sql = ('SELECT dr.*, d.tab_title, n.name AS file_name FROM drift dr '
               'LEFT JOIN dataset d ON d.id = dr.dataset_id '
               'LEFT JOIN node n ON n.file_id = d.file_id WHERE 1=1')
        args: list = []
        if run_id is not None:
            sql += ' AND dr.run_id = ?'
            args.append(run_id)
        if drift_type:
            sql += ' AND dr.drift_type = ?'
            args.append(drift_type)
        return self.conn.execute(sql + ' ORDER BY dr.id', args).fetchall()

    def drift_summary(self, run_id: Optional[int] = None) -> dict:
        sql = 'SELECT drift_type, COUNT(*) n FROM drift'
        args: list = []
        if run_id is not None:
            sql += ' WHERE run_id = ?'
            args.append(run_id)
        rows = self.conn.execute(sql + ' GROUP BY drift_type ORDER BY n DESC',
                                 args).fetchall()
        return {r['drift_type']: r['n'] for r in rows}

    # ------------------------------------------------------------------ #
    # change cursor
    # ------------------------------------------------------------------ #
    def get_cursor(self, subject: str) -> str:
        row = self.conn.execute(
            'SELECT page_token FROM cursor WHERE subject = ?', (subject,)).fetchone()
        return row['page_token'] if row and row['page_token'] else ''

    def set_cursor(self, subject: str, token: str, now: str) -> None:
        self.conn.execute(
            'INSERT INTO cursor (subject, page_token, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT(subject) DO UPDATE SET page_token = excluded.page_token, '
            'updated_at = excluded.updated_at', (subject, token, now))
        self.conn.commit()


def _clip(value: Any, limit: int = 500) -> Optional[str]:
    """Drift reports quote real cell values; a 2MB cell must not become a 2MB row."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f'... (+{len(text) - limit})'
