"""Supabase/Postgres store — the server-side "full index build" path.

Writes an IndexResult into the symbols/edges/chunks tables of schema.sql and runs
the blast-radius reverse walk as a recursive CTE over `edges` (the query
schema.sql's note describes). Connects straight to Postgres via SUPABASE_DB_URL
using psycopg — the trusted server path that bypasses RLS (it sets which client a
write/read is for), exactly as schema.sql's service_role note anticipates.

Never stores raw source: only `chunks.content_hash` + `embedding` are written,
never `Chunk.text`.

This module is imported lazily (psycopg is only needed for this path); the
standalone CLI never touches it.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

from ..model import ChangedSymbol, Chunk, Edge, IndexResult, Symbol, KIND_MODULE


def _repo_hash(client_key: str, full_name: str) -> str:
    """Mirror telemetry.hash_repo: salted, non-reversible repo id that correlates
    the index side with the (hashed) telemetry side."""
    salt = client_key or "unsalted"
    digest = hashlib.sha256(f"{salt}:{full_name}".encode()).hexdigest()
    return f"sha256:{digest[:32]}"


def _vec_literal(embedding: Optional[list[float]]) -> Optional[str]:
    if embedding is None:
        return None
    return "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"


def _parse_vec(raw) -> Optional[list[float]]:
    """Inverse of _vec_literal. pgvector columns come back over psycopg as a
    text literal like '[0.1,0.2,...]' (no vector adapter registered); tolerate a
    list too in case one ever is."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return None
    return [float(x) for x in s.split(",")]


class SupabaseStore:
    """Thin persistence + query layer over Postgres. Open with a `with` block."""

    def __init__(self, db_url: Optional[str] = None):
        url = db_url or os.environ.get("SUPABASE_DB_URL", "").strip()
        if not url:
            raise RuntimeError("SUPABASE_DB_URL not set; cannot open SupabaseStore")
        import psycopg  # lazy
        self._psycopg = psycopg
        self._conn = psycopg.connect(url, autocommit=False)

    def __enter__(self) -> "SupabaseStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @property
    def conn(self):
        """The live psycopg connection. Exposed so a caller (e.g. zenik-platform)
        can run its own platform-owned SQL — impact_history, telemetry, repo
        lookups — on the same trusted, RLS-bypassing connection rather than
        opening a second one. The domain write/read paths below stay the
        canonical way to touch symbols/edges/chunks."""
        return self._conn

    # -- tenancy ------------------------------------------------------------
    def ensure_client(self, client_key: str, name: Optional[str] = None) -> str:
        with self._conn.cursor() as cur:
            cur.execute(
                "insert into clients (client_key, name) values (%s, %s) "
                "on conflict (client_key) do update set name = coalesce(excluded.name, clients.name) "
                "returning id",
                (client_key, name),
            )
            cid = cur.fetchone()[0]
        self._conn.commit()
        return str(cid)

    def ensure_repo(self, client_id: str, full_name: str, client_key: str,
                    default_branch: str = "main") -> str:
        rhash = _repo_hash(client_key, full_name)
        with self._conn.cursor() as cur:
            cur.execute(
                "insert into repos (client_id, full_name, repo_hash, default_branch) "
                "values (%s, %s, %s, %s) "
                "on conflict (client_id, repo_hash) do update set full_name = excluded.full_name "
                "returning id",
                (client_id, full_name, rhash, default_branch),
            )
            rid = cur.fetchone()[0]
        self._conn.commit()
        return str(rid)

    # -- incremental support -------------------------------------------------
    def chunk_hashes(self, repo_id: str) -> list[str]:
        """The content hashes of a repo's stored chunks. The client compares
        these against its freshly parsed chunks and embeds ONLY the hashes we
        don't already have — the heart of incremental indexing."""
        with self._conn.cursor() as cur:
            cur.execute(
                "select distinct content_hash from chunks "
                "where repo_id = %s and embedding is not null",
                (repo_id,),
            )
            rows = [r[0] for r in cur.fetchall()]
        self._conn.commit()  # end the read transaction (autocommit is off)
        return rows

    # -- write path ---------------------------------------------------------
    def replace_index(self, repo_id: str, result: IndexResult) -> dict:
        """Replace a repo's index. Symbols/edges are rewritten wholesale (they
        are small); chunks that arrive WITHOUT an embedding reuse the stored
        embedding for the same content_hash, so an incremental push only ships
        vectors for genuinely new/changed chunks. Runs in one transaction,
        threading Symbol.key() -> uuid so edges/chunks resolve correctly."""
        from psycopg.rows import tuple_row
        conn = self._conn
        try:
            with conn.cursor(row_factory=tuple_row) as cur:
                # Before the delete: grab stored embeddings for the hashes the
                # payload omitted, so unchanged chunks keep their vectors.
                missing = list({c.content_hash for c in result.chunks
                                if c.embedding is None and c.content_hash})
                reuse: dict[str, str] = {}
                if missing:
                    cur.execute(
                        "select distinct on (content_hash) content_hash, "
                        "embedding::text from chunks "
                        "where repo_id = %s and content_hash = any(%s) "
                        "and embedding is not null",
                        (repo_id, missing),
                    )
                    reuse = {h: emb for h, emb in cur.fetchall()}
                # Full rebuild: clear existing rows for this repo. edges/chunks have
                # ON DELETE CASCADE from symbols, but delete them explicitly too so
                # chunks whose symbol_id is null (module-less) also go.
                cur.execute("delete from chunks where repo_id = %s", (repo_id,))
                cur.execute("delete from edges  where repo_id = %s", (repo_id,))
                cur.execute("delete from symbols where repo_id = %s", (repo_id,))

                # symbols — insert in order, RETURNING id in the same order so we
                # can zip keys to uuids.
                key_to_id: dict[str, str] = {}
                if result.symbols:
                    values = [
                        (repo_id, s.kind, s.name, s.path, s.start_line, s.end_line,
                         s.language, s.commit_sha)
                        for s in result.symbols
                    ]
                    ids = self._insert_returning(
                        cur,
                        "insert into symbols "
                        "(repo_id, kind, name, path, start_line, end_line, language, commit_sha) values ",
                        "(%s,%s,%s,%s,%s,%s,%s,%s)",
                        values,
                    )
                    for s, sid in zip(result.symbols, ids):
                        key_to_id[s.key()] = str(sid)

                # edges — map keys to uuids; drop edges whose src didn't resolve.
                edge_rows = []
                for e in result.edges:
                    src_id = key_to_id.get(e.src)
                    if not src_id:
                        continue
                    dst_id = key_to_id.get(e.dst) if e.dst else None
                    edge_rows.append((repo_id, src_id, dst_id, e.dst_name,
                                      e.edge_type, e.confidence))
                if edge_rows:
                    self._insert_values(
                        cur,
                        "insert into edges "
                        "(repo_id, src_symbol_id, dst_symbol_id, dst_name, edge_type, confidence) values ",
                        "(%s,%s,%s,%s,%s,%s)",
                        edge_rows,
                    )

                # chunks — embedding as a vector literal; symbol_id nullable.
                # A chunk without an embedding falls back to the stored vector
                # for its content_hash (incremental push).
                chunk_rows = []
                reused = 0
                for c in result.chunks:
                    sym_id = key_to_id.get(c.symbol) if c.symbol else None
                    emb = _vec_literal(c.embedding)
                    if emb is None and c.content_hash in reuse:
                        emb = reuse[c.content_hash]
                        reused += 1
                    chunk_rows.append((repo_id, sym_id, c.path, c.start_line,
                                       c.end_line, c.language, c.content_hash,
                                       emb))
                if chunk_rows:
                    self._insert_values(
                        cur,
                        "insert into chunks "
                        "(repo_id, symbol_id, path, start_line, end_line, language, content_hash, embedding) values ",
                        "(%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                        chunk_rows,
                    )

                # record the indexed commit
                if result.commit_sha:
                    cur.execute(
                        "update repos set last_indexed_sha = %s where id = %s",
                        (result.commit_sha, repo_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"symbols": len(result.symbols), "edges": len(result.edges),
                "chunks": len(result.chunks), "embeddings_reused": reused}

    # -- read path: rehydrate the stored index ------------------------------
    def load_index(self, repo_id: str) -> IndexResult:
        """Reconstruct a repo's stored index back into an in-memory IndexResult.

        The platform runs blast radius through the SAME `ImpactEngine` the
        standalone path uses (so results can't drift from a second SQL copy of
        the walk). That engine needs an IndexResult, so we read symbols/edges/
        chunks back and rebuild the Symbol.key() identities the engine indexes
        on (`path::name::start_line`): edges' src/dst uuids become keys, chunks'
        symbol_id becomes a key, and embeddings are parsed back to float lists so
        the semantic half still works from the already-embedded stored chunks.

        Each rebuilt Symbol carries its DB uuid in `.id`, so the caller can map
        impact results back to rows (e.g. for impact_history)."""
        conn = self._conn
        with conn.cursor() as cur:
            cur.execute(
                "select id, kind, name, path, start_line, end_line, language, commit_sha "
                "from symbols where repo_id = %s",
                (repo_id,),
            )
            sym_rows = cur.fetchall()

        id_to_key: dict[str, str] = {}
        symbols: list[Symbol] = []
        for sid, kind, name, path, start_line, end_line, language, commit_sha in sym_rows:
            s = Symbol(
                name=name, kind=kind or "", path=path, language=language or "",
                start_line=start_line or 0, end_line=end_line or 0,
                commit_sha=commit_sha, id=str(sid),
            )
            id_to_key[str(sid)] = s.key()
            symbols.append(s)

        with conn.cursor() as cur:
            cur.execute(
                "select src_symbol_id, dst_symbol_id, dst_name, edge_type, confidence "
                "from edges where repo_id = %s",
                (repo_id,),
            )
            edge_rows = cur.fetchall()

        edges: list[Edge] = []
        for src_id, dst_id, dst_name, edge_type, confidence in edge_rows:
            src_key = id_to_key.get(str(src_id))
            if not src_key:
                continue  # src symbol gone; edge is meaningless
            dst_key = id_to_key.get(str(dst_id)) if dst_id else None
            edges.append(Edge(
                src=src_key, edge_type=edge_type, dst=dst_key,
                dst_name=dst_name,
                confidence=float(confidence) if confidence is not None else 1.0,
            ))

        with conn.cursor() as cur:
            cur.execute(
                "select symbol_id, path, start_line, end_line, language, content_hash, embedding "
                "from chunks where repo_id = %s",
                (repo_id,),
            )
            chunk_rows = cur.fetchall()

        chunks: list[Chunk] = []
        for sym_id, path, start_line, end_line, language, content_hash, embedding in chunk_rows:
            chunks.append(Chunk(
                path=path, language=language or "",
                start_line=start_line or 0, end_line=end_line or 0,
                content_hash=content_hash,
                symbol=id_to_key.get(str(sym_id)) if sym_id else None,
                text=None, embedding=_parse_vec(embedding),
            ))

        with conn.cursor() as cur:
            cur.execute(
                "select full_name, last_indexed_sha from repos where id = %s",
                (repo_id,),
            )
            row = cur.fetchone()
        repo_path = row[0] if row else ""
        commit_sha = row[1] if row else None

        return IndexResult(
            repo_path=repo_path, commit_sha=commit_sha,
            symbols=symbols, edges=edges, chunks=chunks,
        )

    # -- bulk-insert helpers ------------------------------------------------
    def _insert_returning(self, cur, prefix: str, row_tmpl: str, values: list, batch: int = 500):
        """Multi-row INSERT ... RETURNING id, batched, preserving input order."""
        ids: list = []
        for i in range(0, len(values), batch):
            chunk = values[i:i + batch]
            placeholders = ",".join([row_tmpl] * len(chunk))
            flat = [v for row in chunk for v in row]
            cur.execute(prefix + placeholders + " returning id", flat)
            ids.extend(r[0] for r in cur.fetchall())
        return ids

    def _insert_values(self, cur, prefix: str, row_tmpl: str, values: list, batch: int = 500):
        for i in range(0, len(values), batch):
            chunk = values[i:i + batch]
            placeholders = ",".join([row_tmpl] * len(chunk))
            flat = [v for row in chunk for v in row]
            cur.execute(prefix + placeholders, flat)

    # -- read path: blast radius -------------------------------------------
    def reverse_walk(self, repo_id: str, seed_symbol_ids: list[str],
                     max_depth: int = 3) -> list[dict]:
        """Recursive CTE over `edges`, seeded by changed symbol ids, walking
        dst->src (who depends on me) to a bounded depth. This is the deterministic
        half of blast radius, run in the DB. Returns impacted symbol rows with the
        depth and edge_type they were first reached at."""
        if not seed_symbol_ids:
            return []
        sql = """
        with recursive walk(symbol_id, depth, edge_type, confidence) as (
            select e.src_symbol_id, 1, e.edge_type, e.confidence
              from edges e
             where e.repo_id = %(repo)s
               and e.dst_symbol_id = any(%(seeds)s::uuid[])
          union all
            select e.src_symbol_id, w.depth + 1, e.edge_type, e.confidence
              from edges e
              join walk w on e.dst_symbol_id = w.symbol_id
             where e.repo_id = %(repo)s
               and w.depth < %(depth)s
        )
        select w.symbol_id,
               min(w.depth)                       as depth,
               (array_agg(w.edge_type order by w.depth))[1] as reason,
               max(w.confidence)                  as confidence,
               s.name, s.path, s.kind, s.language, s.start_line, s.end_line
          from walk w
          join symbols s on s.id = w.symbol_id
         where not (w.symbol_id = any(%(seeds)s::uuid[]))
           and s.kind <> 'module'   -- module symbols are graph glue, not review targets
         group by w.symbol_id, s.name, s.path, s.kind, s.language, s.start_line, s.end_line
         order by depth asc, confidence desc
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, {"repo": repo_id, "seeds": seed_symbol_ids, "depth": max_depth})
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def semantic_neighbors(self, repo_id: str, query_embedding: list[float],
                           limit: int = 25) -> list[dict]:
        """pgvector cosine-NN over `chunks.embedding` — the semantic half. Uses the
        `<=>` cosine-distance operator (smaller = closer)."""
        sql = """
        select c.symbol_id, c.path, c.start_line, c.end_line, c.language,
               1 - (c.embedding <=> %(q)s::vector) as similarity,
               s.name, s.kind
          from chunks c
          left join symbols s on s.id = c.symbol_id
         where c.repo_id = %(repo)s and c.embedding is not null
         order by c.embedding <=> %(q)s::vector asc
         limit %(limit)s
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, {"repo": repo_id, "q": _vec_literal(query_embedding), "limit": limit})
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def delete_repo(self, repo_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("delete from repos where id = %s", (repo_id,))
        self._conn.commit()
