"""
Types for what a route accepts.

A row id arrives as digits in a path. Without a bound, `/memory/99999999999999999999` is a Python
int too big for SQLite, which raises while binding it and the client gets a 500 for what is
plainly a 404. SQLite rowids are 64-bit; the bound here is the largest integer a browser can hold
exactly, which is the largest a screen could ever have sent.
"""

from typing import Annotated

from fastapi import Path

RowId = Annotated[int, Path(ge=1, le=2**53 - 1)]
