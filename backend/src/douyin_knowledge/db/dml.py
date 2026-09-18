"""Row counts from bulk INSERT/UPDATE/DELETE.

`Session.execute` is annotated as returning `Result[Any]`, which has no `rowcount`, even
though a DML statement returns a `CursorResult` at runtime -- SQLAlchemy cannot express
"the return type depends on the kind of statement" in its overloads. Three call sites were
reading `result.rowcount` off the declared type and getting away with it only because
nothing type-checked them.

Narrowing once here, rather than casting at each call site, keeps the reason for the cast in
one place: the cast is safe *because* the argument is a DML statement, and that precondition
is worth stating once instead of trusting three separate comments to stay accurate.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, Delete, Insert, Update
from sqlalchemy.orm import Session


def execute_rowcount(session: Session, statement: Delete | Update | Insert) -> int:
    """Run a DML statement and report how many rows it affected.

    Returns 0 rather than -1 when the driver declines to report a count, so callers can
    treat the result as a plain "how many changed" without special-casing sqlite quirks.
    """
    result = cast(CursorResult[object], session.execute(statement))
    count = result.rowcount
    return int(count) if count and count > 0 else 0
