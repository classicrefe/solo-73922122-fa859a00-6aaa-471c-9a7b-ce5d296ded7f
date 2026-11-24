"""CSV parsing and the reference-graph algorithms.

Graph model
-----------
Each plate node has directed edges ``plate -> ref_number`` for every number in
its reference list.  A referenced number without a ``plates`` row is a
*missing piece*.

A plate is servable iff it cannot reach any missing piece along outgoing
reference edges.  Equivalently, start reverse traversal from every missing
piece: every plate reached by that traversal is suspended (directly or
indirectly references a missing / withdrawn plate); every other plate is
serving.  Pure mutual-reference cycles that never reach a missing piece are
therefore all serving ("齐备的互引图版一同供出").

Incremental scope
-----------------
For a batch of changed plate numbers, the recomputation scope is the
undirected closure reached from those numbers in the graph formed by the
union of:

* all reference edges as they were *before* the change, and
* the new outgoing reference lists of the upserted rows (*after* the change).

Traversal follows both outgoing and incoming old edges plus the new edges.
Consequently any edge (old or new) incident to a scoped node keeps its other
endpoint inside the scope, so post-change no edge crosses the scope boundary
and statuses outside the scope cannot change.  Only the scoped plates are
recomputed and republished.
"""
from __future__ import annotations

import csv
import io
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

# Separators accepted inside the "reference list" cell (and between the
# trailing cells when a wide CSV is supplied).  Plain whitespace is included
# so space-separated numbers work; catalog numbers therefore must not contain
# spaces.
REF_SPLIT_RE = re.compile(r"[;；|｜,，、\r\n\t ]+")


def split_refs(value: object) -> list[str]:
    """Split a reference-list cell into de-duplicated, order-kept numbers."""
    if value is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in REF_SPLIT_RE.split(str(value)):
        token = token.strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


@dataclass
class CatalogRow:
    number: str
    title: str = ""
    refs: list[str] = field(default_factory=list)
    line_no: int = 0


@dataclass
class ParseIssue:
    line_no: int
    message: str


@dataclass
class ParseResult:
    rows: list[CatalogRow]
    errors: list[ParseIssue]
    warnings: list[str]
    encoding: str = "utf-8"

    @property
    def ok(self) -> bool:
        return not self.errors


_NUMBER_ALIASES = (
    "图版号", "图号", " plateid", "plate id", "platenumber", "plate number",
    "编号", "序号", "number", "plate", "id",
)
_TITLE_ALIASES = ("题名", "名称", "标题", "title", "name")
_REF_ALIASES = (
    "引用号列表", "引用号", "引用编号", "引用图版", "参考号", "参考文献",
    "关联图版", "refs", "references", "reference", "ref", "related",
)


def _normalise_header(cell: object) -> str:
    return re.sub(r"[\s_\-]+", "", str(cell or "")).lower()


def _matches_any(header_norm: str, aliases: Iterable[str]) -> bool:
    for alias in aliases:
        a = _normalise_header(alias)
        if header_norm == a or a in header_norm:
            return True
    return False


def decode_catalog_bytes(data: bytes) -> tuple[str, str]:
    """Decode uploaded CSV bytes, tolerating Excel's GBK output."""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def parse_catalog(data: bytes | str) -> ParseResult:
    """Parse a catalog CSV upload into :class:`CatalogRow` objects.

    Accepted layouts:

    * header ``图版号,题名,引用号`` with the ref list separated by ``; | ，``
      etc. (commas work when the field is quoted);
    * wide form ``图版号,题名,引用号1,引用号2,...``;
    * header-less positional ``number,title,ref1,ref2,...``.
    """
    if isinstance(data, bytes):
        text, encoding = decode_catalog_bytes(data)
    else:
        text, encoding = data, "utf-8"

    reader = csv.reader(io.StringIO(text))
    records = list(reader)
    if not records:
        return ParseResult([], [ParseIssue(0, "文件为空")], [], encoding)

    # Locate the first non-empty record; decide whether it is a header.
    first_idx = 0
    while first_idx < len(records) and not any(c.strip() for c in records[first_idx]):
        first_idx += 1
    if first_idx >= len(records):
        return ParseResult([], [ParseIssue(0, "文件为空")], [], encoding)

    first = records[first_idx]
    first_norm = [_normalise_header(c) for c in first]
    has_header = any(_matches_any(h, _NUMBER_ALIASES) for h in first_norm)

    errors: list[ParseIssue] = []
    warnings: list[str] = []
    raw_rows: list[CatalogRow] = []

    if has_header:
        num_col = next(
            (i for i, h in enumerate(first_norm) if _matches_any(h, _NUMBER_ALIASES)),
            0,
        )
        title_col = next(
            (i for i, h in enumerate(first_norm) if _matches_any(h, _TITLE_ALIASES) and i != num_col),
            None,
        )
        ref_cols = [
            i for i, h in enumerate(first_norm) if _matches_any(h, _REF_ALIASES)
        ]
        if not ref_cols:
            claimed = {num_col}
            if title_col is not None:
                claimed.add(title_col)
            ref_cols = [i for i in range(len(first)) if i not in claimed]
        data_start = first_idx + 1
    else:
        num_col = 0
        title_col = 1 if len(first) > 1 else None
        ref_cols = list(range(2, max(len(first), 2)))
        data_start = first_idx

    seen_numbers: dict[str, int] = {}

    # csv reader line numbers track physical source lines.
    line_no = 0
    for idx in range(data_start, len(records)):
        record = records[idx]
        line_no = idx + 1
        if not record or not any(str(c).strip() for c in record):
            continue  # blank line

        number = record[num_col].strip() if num_col < len(record) else ""
        if not number:
            errors.append(ParseIssue(line_no, "图版号为空"))
            continue

        title = ""
        if title_col is not None and title_col < len(record):
            title = record[title_col].strip()

        refs: list[str] = []
        for col in ref_cols:
            if col < len(record):
                refs.extend(split_refs(record[col]))
        # De-duplicate across cells too, preserving first occurrence order.
        deduped: list[str] = []
        seen_refs: set[str] = set()
        for r in refs:
            if r not in seen_refs:
                seen_refs.add(r)
                deduped.append(r)

        if number in seen_numbers:
            warnings.append(
                f"图版号 {number} 在文件中出现多次，以最后一行为准（整条替换）"
            )
        seen_numbers[number] = line_no
        raw_rows.append(CatalogRow(number=number, title=title, refs=deduped, line_no=line_no))

    if errors:
        return ParseResult([], errors, warnings, encoding)

    # Last occurrence wins for duplicate numbers inside one file.
    merged: dict[str, CatalogRow] = {}
    for row in raw_rows:
        merged[row.number] = row
    rows = list(merged.values())

    if not rows:
        errors.append(ParseIssue(0, "没有解析到任何数据行"))
    return ParseResult(rows, errors, warnings, encoding)


def strip_comment_lines(text: object) -> str:
    """Remove lines whose first non-blank character is ``#`` (list files)."""
    if text is None:
        return ""
    kept = [
        line
        for line in str(text).splitlines()
        if not line.lstrip().startswith("#")
    ]
    return "\n".join(kept)


def parse_numbers(text: object) -> list[str]:
    """Parse the withdraw textarea / single-number field / number list file."""
    return split_refs(strip_comment_lines(text))


def _old_neighbours(conn, number: str) -> set[str]:
    outgoing = {
        r[0]
        for r in conn.execute(
            "SELECT ref_number FROM refs WHERE plate_number = ?", (number,)
        )
    }
    incoming = {
        r[0]
        for r in conn.execute(
            "SELECT plate_number FROM refs WHERE ref_number = ?", (number,)
        )
    }
    return outgoing | incoming


def compute_scope(
    conn,
    changed_numbers: Iterable[str],
    new_refs: dict[str, list[str]] | None = None,
) -> set[str]:
    """Undirected union-graph closure around the changed plate numbers.

    Reads the *old* state from the database and overlays the incoming new
    reference lists.  Safe to call inside the write transaction before the
    rows are replaced.
    """
    new_refs = new_refs or {}
    scope: set[str] = {n for n in changed_numbers if n}
    queue: deque[str] = deque(scope)

    while queue:
        number = queue.popleft()
        neighbours = _old_neighbours(conn, number)
        if number in new_refs:
            neighbours.update(new_refs[number])
        for neighbour in neighbours:
            if neighbour and neighbour not in scope:
                scope.add(neighbour)
                queue.append(neighbour)
    return scope


def recompute_states(
    conn, scope: set[str]
) -> dict[str, tuple[str, str | None, list[str] | None]]:
    """Recompute serving/suspended state for plates in ``scope``.

    Returns ``{plate_number: (state, blocker, witness_chain)}`` for every
    *existing* plate in scope.  Missing numbers are represented only inside
    witness chains.
    """
    if not scope:
        return {}

    scope_list = sorted(scope)
    placeholders = ",".join("?" for _ in scope_list)
    existing = {
        r[0]
        for r in conn.execute(
            f"SELECT number FROM plates WHERE number IN ({placeholders})",
            tuple(scope_list),
        )
    }
    missing = scope - existing

    # witness[node] = deterministic chain from node to a missing piece.
    witness: dict[str, list[str]] = {m: [m] for m in missing}
    queue: deque[str] = deque(sorted(missing))

    while queue:
        current = queue.popleft()
        parents = [
            r[0]
            for r in conn.execute(
                "SELECT plate_number FROM refs WHERE ref_number = ? "
                "ORDER BY plate_number",
                (current,),
            )
        ]
        for parent in parents:
            if parent in scope and parent not in witness:
                witness[parent] = [parent] + witness[current]
                queue.append(parent)

    states: dict[str, tuple[str, str | None, list[str] | None]] = {}
    for number in existing:
        if number in witness:
            chain = witness[number]
            states[number] = ("suspended", chain[-1], chain)
        else:
            states[number] = ("serving", None, None)
    return states
