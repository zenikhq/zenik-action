"""Persistence backends for an index.

Two stores, one interface-in-spirit:

  * json_store  — serialize an IndexResult to a local file. The standalone path;
    needs no network or DB. Chunk source text is dropped on write (only the
    derived embedding is kept), so even the local artifact never holds raw source.

  * SupabaseStore — write an index into Postgres/pgvector matching schema.sql and
    run the blast-radius query there. This is the "server-side full index build,
    called by the platform" path.
"""
from .json_store import load_index, save_index

__all__ = ["load_index", "save_index"]
