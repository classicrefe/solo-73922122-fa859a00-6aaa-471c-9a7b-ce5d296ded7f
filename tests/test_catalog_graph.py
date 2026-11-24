# -*- coding: utf-8 -*-
"""Core serving semantics, incremental scope and atomic publishing."""
import json

import pytest

from app import catalog
from app.db import active_version
from app.graph import CatalogRow, compute_scope


def R(number, title="", refs=None):
    return CatalogRow(number=number, title=title or number, refs=refs or [])


def upload(conn, rows, source="test"):
    return catalog.apply_changes(conn, upserts=rows, source=source)


def numbers_in_index(conn, version=None):
    version = version or active_version(conn)
    return {
        r[0]
        for r in conn.execute(
            "SELECT plate_number FROM index_entries WHERE version = ?", (version,)
        )
    }


def state_of(conn, number):
    row = conn.execute(
        "SELECT state FROM plate_status WHERE plate_number = ?", (number,)
    ).fetchone()
    return row["state"] if row else None


def test_missing_piece_suspends_direct_and_transitive_referrers(conn):
    upload(conn, [
        R("A1", refs=["A2", "MISSING"]),
        R("A2"),
        R("Z1", refs=["A1"]),   # indirect reference to the missing piece
        R("B1", refs=["B2"]),
        R("B2"),
    ])
    assert numbers_in_index(conn) == {"A2", "B1", "B2"}
    assert state_of(conn, "A1") == "suspended"
    assert state_of(conn, "Z1") == "suspended"
    assert state_of(conn, "A2") == "serving"
    assert state_of(conn, "B1") == "serving"

    row = conn.execute(
        "SELECT witness FROM plate_status WHERE plate_number = 'Z1'"
    ).fetchone()
    chain = json.loads(row["witness"])
    assert chain[0] == "Z1" and chain[-1] == "MISSING"


def test_mutual_reference_cycle_fully_available(conn):
    upload(conn, [
        R("X1", refs=["X2"]),
        R("X2", refs=["X3"]),
        R("X3", refs=["X1", "X2"]),
    ])
    assert numbers_in_index(conn) == {"X1", "X2", "X3"}


def test_cycle_reaching_missing_is_suspended(conn):
    upload(conn, [
        R("X1", refs=["X2"]),
        R("X2", refs=["X3"]),
        R("X3", refs=["X1", "GONE"]),
    ])
    assert numbers_in_index(conn) == set()
    for n in ("X1", "X2", "X3"):
        assert state_of(conn, n) == "suspended"


def test_withdraw_suspends_referrers_then_refill_recovers(conn):
    upload(conn, [
        R("A1", refs=["A2", "A3"]),
        R("A2"),
        R("A3", refs=["A4"]),
        R("A4", refs=["A2"]),
        R("Z1", refs=["A1"]),
        R("B1", refs=["B2"]),
        R("B2"),
    ])
    assert len(numbers_in_index(conn)) == 7

    report = catalog.apply_changes(conn, withdrawals=["A3"], source="withdraw")
    # Old undirected component of A3 covers the A group, never B.
    assert set(report.scope) == {"A1", "A2", "A3", "A4", "Z1"}
    assert numbers_in_index(conn) == {"A2", "A4", "B1", "B2"}
    assert state_of(conn, "A1") == "suspended"
    assert state_of(conn, "Z1") == "suspended"
    assert state_of(conn, "A4") == "serving"
    assert conn.execute("SELECT 1 FROM plates WHERE number='A3'").fetchone() is None

    # Refill the missing piece -> automatic recovery.
    report2 = upload(conn, [R("A3", "补录的A3", ["A4"])])
    assert set(report2.scope) == {"A1", "A2", "A3", "A4", "Z1"}
    assert numbers_in_index(conn) == {"A1", "A2", "A3", "A4", "Z1", "B1", "B2"}


def test_same_number_is_whole_record_replacement(conn):
    upload(conn, [R("A1", "旧题名", ["A2", "A3"]), R("A2"), R("A3")])
    assert state_of(conn, "A1") == "serving"

    # Replace A1 with no references and a new title.
    upload(conn, [R("A1", "新题名", [])])
    plate = conn.execute("SELECT title FROM plates WHERE number='A1'").fetchone()
    assert plate["title"] == "新题名"
    refs = [r[0] for r in conn.execute(
        "SELECT ref_number FROM refs WHERE plate_number='A1' ORDER BY position")]
    assert refs == []
    assert numbers_in_index(conn) == {"A1", "A2", "A3"}

    # Withdrawing A2 now must NOT touch A1 (edge was removed by replacement).
    catalog.apply_changes(conn, withdrawals=["A2"])
    assert state_of(conn, "A1") == "serving"


def test_scope_is_undirected_union_of_old_and_new_edges(conn):
    upload(conn, [
        R("A1", refs=["A2"]), R("A2"),
        R("B1", refs=["B2"]), R("B2"),
    ])

    # Cosmetic re-upload of A1: scope stays inside component A.
    report = upload(conn, [R("A1", refs=["A2"])])
    assert set(report.scope) == {"A1", "A2"}

    # New edge A1 -> B1 pulls component B into scope via AFTER-change refs.
    report = upload(conn, [R("A1", refs=["A2", "B1"])])
    assert set(report.scope) == {"A1", "A2", "B1", "B2"}

    # Removing B1 again: the OLD edge A1-B1 counts, and traversal continues
    # along the old B1-B2 edge (direction unrestricted), so B2 is included too.
    report = upload(conn, [R("A1", refs=["A2"])])
    assert set(report.scope) == {"A1", "A2", "B1", "B2"}

    # Direct compute_scope check for incoming old edges.
    scope = compute_scope(conn, ["B2"], {})
    assert scope == {"B2", "B1"}


def test_failed_apply_rolls_back_plates_and_keeps_old_index(conn):
    upload(conn, [R("OK1", refs=["OK2"]), R("OK2")])
    version_before = active_version(conn)
    entries_before = {
        r["plate_number"]: (r["title"], r["refs_text"])
        for r in conn.execute(
            "SELECT plate_number, title, refs_text FROM index_entries "
            "WHERE version = ?", (version_before,))
    }

    def boom():
        raise RuntimeError("simulated publish failure")

    with pytest.raises(RuntimeError):
        catalog.apply_changes(
            conn, upserts=[R("BAD", refs=["NOPE"])], fail_hook=boom
        )

    # Pointer unchanged and old published rows untouched.
    assert active_version(conn) == version_before
    entries_after = {
        r["plate_number"]: (r["title"], r["refs_text"])
        for r in conn.execute(
            "SELECT plate_number, title, refs_text FROM index_entries "
            "WHERE version = ?", (version_before,))
    }
    assert entries_after == entries_before
    # Rolled-back plate write.
    assert conn.execute("SELECT 1 FROM plates WHERE number='BAD'").fetchone() is None
    # Database remains usable after rollback.
    report = upload(conn, [R("AFTER", refs=[])])
    assert report.version == version_before + 1
    assert "AFTER" in numbers_in_index(conn)
