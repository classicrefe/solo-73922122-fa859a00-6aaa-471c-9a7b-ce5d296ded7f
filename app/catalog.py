"""Catalog mutations and atomic publishing of the serving index."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from . import graph
from .db import active_version, now_str

# Catalog writes are serialised inside this process; the Docker deployment
# runs a single gunicorn worker anyway.
_WRITE_LOCK = threading.RLock()


class CatalogError(Exception):
    """Raised when a change cannot be applied; the old index stays active."""


@dataclass
class ChangeReport:
    action: str
    changed: list[str] = field(default_factory=list)
    withdrawals: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    recomputed: list[str] = field(default_factory=list)
    version: int = 0
    serving_count: int = 0
    detail: str = ""


def _reason_for(witness: list[str] | None) -> str:
    if not witness:
        return ""
    chain = " → ".join(witness[:-1]) + f" → 缺件「{witness[-1]}」"
    return "引用链上存在缺件：" + chain


def _publish_new_version(
    conn,
    *,
    scope: set[str],
    states: dict[str, tuple[str, str | None, list[str] | None]],
    changed: list[str],
    source: str,
    now: str,
) -> int:
    old_version = active_version(conn)
    cur = conn.execute(
        "INSERT INTO index_versions (created_at, source, changed_json, scope_json, recomputed) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            now,
            source,
            json.dumps(changed, ensure_ascii=False),
            json.dumps(sorted(scope), ensure_ascii=False),
            len(states),
        ),
    )
    new_version = cur.lastrowid

    # Carry forward entries outside the recomputed scope untouched.
    scope_list = sorted(scope)
    placeholders = ",".join("?" for _ in scope_list)
    conn.execute(
        f"""
        INSERT INTO index_entries (version, plate_number, title, refs_text)
        SELECT ?, plate_number, title, refs_text
          FROM index_entries
         WHERE version = ?
           AND plate_number NOT IN ({placeholders})
        """,
        (new_version, old_version, *scope_list),
    )

    # Republish fresh rows for scoped plates whose new state is serving.
    serving_numbers = sorted(n for n, st in states.items() if st[0] == "serving")
    for number in serving_numbers:
        plate = conn.execute(
            "SELECT title FROM plates WHERE number = ?", (number,)
        ).fetchone()
        ref_rows = conn.execute(
            "SELECT ref_number FROM refs WHERE plate_number = ? ORDER BY position",
            (number,),
        ).fetchall()
        refs_text = ";".join(r[0] for r in ref_rows)
        conn.execute(
            "INSERT INTO index_entries (version, plate_number, title, refs_text) "
            "VALUES (?, ?, ?, ?)",
            (new_version, number, plate["title"] if plate else "", refs_text),
        )

    count_row = conn.execute(
        "SELECT COUNT(*) AS c FROM index_entries WHERE version = ?", (new_version,)
    ).fetchone()
    conn.execute(
        "UPDATE index_versions SET serving_count = ? WHERE version = ?",
        (count_row["c"], new_version),
    )
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'active_index_version'",
        (str(new_version),),
    )
    return new_version


def apply_changes(
    conn,
    *,
    upserts: list[graph.CatalogRow] | None = None,
    withdrawals: list[str] | None = None,
    source: str = "",
    action: str = "upload",
    fail_hook=None,
) -> ChangeReport:
    """Apply whole-record upserts and/or number withdrawals atomically.

    The scope is computed from the old graph plus the incoming refs, then
    rows/status/index-version are written and the active pointer flipped in a
    single ``BEGIN IMMEDIATE`` transaction.  Any exception rolls the whole
    thing back, leaving the previously published index live.
    """
    upserts = upserts or []
    withdrawals = list(dict.fromkeys(w for w in (withdrawals or []) if w))
    upsert_map: dict[str, graph.CatalogRow] = {}
    for row in upserts:
        upsert_map[row.number] = row

    overlap = sorted(set(upsert_map) & set(withdrawals))
    if overlap:
        raise CatalogError(
            "同一批变动中以下图版号既补录又撤下，无法确定意图：" + ", ".join(overlap)
        )

    changed = sorted(set(upsert_map) | set(withdrawals))
    if not changed:
        raise CatalogError("没有需要处理的图版号")

    new_refs = {number: row.refs for number, row in upsert_map.items()}
    report = ChangeReport(
        action=action,
        changed=changed,
        withdrawals=withdrawals,
    )

    with _WRITE_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Scope first, while the old state is still fully present.
            scope = graph.compute_scope(conn, changed, new_refs)
            report.scope = sorted(scope)

            now = now_str()

            # 1) Withdraw whole records by number.
            for number in withdrawals:
                conn.execute("DELETE FROM refs WHERE plate_number = ?", (number,))
                conn.execute(
                    "DELETE FROM plate_status WHERE plate_number = ?", (number,)
                )
                conn.execute("DELETE FROM plates WHERE number = ?", (number,))

            # 2) Whole-record replacement for every uploaded number.
            for number, row in upsert_map.items():
                conn.execute(
                    """
                    INSERT INTO plates (number, title, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(number) DO UPDATE SET
                        title = excluded.title,
                        updated_at = excluded.updated_at
                    """,
                    (number, row.title, now, now),
                )
                conn.execute("DELETE FROM refs WHERE plate_number = ?", (number,))
                conn.executemany(
                    "INSERT INTO refs (plate_number, ref_number, position) "
                    "VALUES (?, ?, ?)",
                    [(number, ref, i) for i, ref in enumerate(row.refs)],
                )

            # 3) Recompute only the connected scope.
            states = graph.recompute_states(conn, scope)
            report.recomputed = sorted(states)
            for number, (state, blocker, witness) in states.items():
                witness_json = json.dumps(witness or [], ensure_ascii=False)
                reason = _reason_for(witness) if state == "suspended" else ""
                conn.execute(
                    """
                    INSERT INTO plate_status
                        (plate_number, state, blocker, witness, reason, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plate_number) DO UPDATE SET
                        state = excluded.state,
                        blocker = excluded.blocker,
                        witness = excluded.witness,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (number, state, blocker, witness_json, reason, now),
                )

            # Test seam for the "failure keeps old index" guarantee.
            if fail_hook is not None:
                fail_hook()

            # 4) Publish a new index version and flip the pointer.
            version = _publish_new_version(
                conn,
                scope=scope,
                states=states,
                changed=changed,
                source=source or action,
                now=now,
            )
            report.version = version
            count_row = conn.execute(
                "SELECT serving_count FROM index_versions WHERE version = ?",
                (version,),
            ).fetchone()
            report.serving_count = count_row["serving_count"]

            detail = (
                f"变动 {len(changed)} 个号；连通圈定 {len(scope)} 个节点；"
                f"重算现存图版 {len(states)} 个；发布索引 v{version}，"
                f"可供 {report.serving_count} 件。"
            )
            report.detail = detail
            conn.execute(
                "INSERT INTO change_log "
                "(created_at, action, changed_json, scope_json, detail, ok) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (
                    now,
                    action,
                    json.dumps(changed, ensure_ascii=False),
                    json.dumps(sorted(scope), ensure_ascii=False),
                    detail,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return report
