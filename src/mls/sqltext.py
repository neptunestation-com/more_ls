"""SQL text helpers shared by the compiler, the identity adapters and the Postgres tools."""
from __future__ import annotations


def q(ident: str) -> str:
    """Quote an identifier: a spec name is the catalog name exactly, whatever its case (the checker
    restricts catalog names to ASCII letters, digits and underscores)."""
    return '"' + ident.replace('"', '""') + '"'
