"""
Camada de acesso ao banco de dados (PostgreSQL via asyncpg).

Esquema:
  search_runs  – registra cada execução de coleta (termo, alvo, resultado).
  posts        – armazena os metadados das postagens coletadas no Instagram.

Observações:
  * O Instagram não expõe API pública estável de comentários, então o coletor
    atual não persiste comentários. O DDL foi escrito de forma que adicionar
    uma tabela `post_comments` no futuro seja trivial (espelhando o padrão
    usado no kwai_collector).
  * `categoria` reflete o tipo de URL classificado pelo runner: post, reel,
    perfil, nao_identificado. Útil para filtrar relatórios.
"""

import hashlib

import asyncpg


# ---------------------------------------------------------------------------
# DDL – criação inicial das tabelas
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS search_runs (
    id          SERIAL       PRIMARY KEY,
    search_term TEXT         NOT NULL,
    target      INTEGER,
    collected   INTEGER      DEFAULT 0,
    started_at  TIMESTAMPTZ  DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS posts (
    id           SERIAL        PRIMARY KEY,
    post_id      VARCHAR(120),
    url          TEXT          NOT NULL UNIQUE,
    categoria    VARCHAR(20),
    author       TEXT,
    verified     BOOLEAN,
    description  TEXT,
    likes        INTEGER       DEFAULT 0,
    comments     INTEGER       DEFAULT 0,
    reposts      INTEGER       DEFAULT 0,
    status       SMALLINT      DEFAULT 0,
    error        TEXT,
    media_url    TEXT,
    media_type   TEXT,
    drive_file_id TEXT,
    drive_url    TEXT,
    published_at TIMESTAMPTZ,
    search_term  TEXT,
    run_id       INTEGER       REFERENCES search_runs(id) ON DELETE SET NULL,
    collected_at TIMESTAMPTZ   DEFAULT NOW(),
    updated_at   TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_posts_search_term ON posts(search_term);
CREATE INDEX IF NOT EXISTS idx_posts_post_id     ON posts(post_id);
CREATE INDEX IF NOT EXISTS idx_posts_categoria   ON posts(categoria);
CREATE INDEX IF NOT EXISTS idx_posts_run_id      ON posts(run_id);

CREATE TABLE IF NOT EXISTS post_comments (
    id           SERIAL       PRIMARY KEY,
    post_id      VARCHAR(120),
    post_url     TEXT         NOT NULL,
    comment_id   TEXT         NOT NULL,
    author       TEXT,
    content      TEXT,
    likes        INTEGER      DEFAULT 0,
    published_at TIMESTAMPTZ,
    collected_at TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (post_url, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_post_comments_post_id
    ON post_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_post_comments_post_url
    ON post_comments(post_url);
"""

# Migrations idempotentes para bancos antigos.
_MIGRATIONS = """
ALTER TABLE posts ADD COLUMN IF NOT EXISTS verified     BOOLEAN;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS reposts      INTEGER DEFAULT 0;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS status       SMALLINT DEFAULT 0;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS error        TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS media_url    TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS media_type   TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS drive_file_id TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS drive_url    TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ DEFAULT NOW();
"""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """Gerencia o pool de conexões e as operações no banco."""

    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 10):
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        """Cria o pool, garante que as tabelas existam e aplica migrations."""
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min,
            max_size=self._max,
            command_timeout=60,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)
            await conn.execute(_MIGRATIONS)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # ------------------------------------------------------------------
    # Execuções de busca
    # ------------------------------------------------------------------

    async def create_run(self, search_term: str, target: int) -> int:
        """Registra o início de uma coleta e retorna o run_id."""
        async with self._pool.acquire() as conn:
            run_id: int = await conn.fetchval(
                """
                INSERT INTO search_runs (search_term, target, started_at)
                VALUES ($1, $2, NOW())
                RETURNING id
                """,
                search_term,
                target,
            )
        return run_id

    async def finish_run(self, run_id: int, collected: int) -> None:
        """Atualiza o run com o total coletado e o horário de término."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE search_runs
                SET collected = $1, finished_at = NOW()
                WHERE id = $2
                """,
                collected,
                run_id,
            )

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------

    async def upsert_posts(
        self,
        posts: list[dict],
        search_term: str,
        run_id: int | None = None,
    ) -> int:
        """Insere ou atualiza posts em lote.

        Cada `post` deve seguir o schema produzido por
        `collector._normalize_record_for_output` (url, post_id, categoria,
        author, verified, description, likes, comments, reposts, status,
        error, published_at).

        Retorna o número de linhas processadas.
        """
        if not posts:
            return 0

        rows = [
            (
                _str(p.get("post_id")),                   # $1
                _str(p.get("url", "")),                   # $2
                _str(p.get("categoria")),                 # $3
                _str(p.get("author")),                    # $4
                p.get("verified") if isinstance(p.get("verified"), bool) else None,  # $5
                _str(p.get("description")),               # $6
                _int(p.get("likes")),                     # $7
                _int(p.get("comments")),                  # $8
                _int(p.get("reposts")),                   # $9
                _int(p.get("status")),                    # $10
                _str(p.get("error")),                     # $11
                _str(p.get("media_url")),                 # $12
                _str(p.get("media_type")),                # $13
                _str(p.get("drive_file_id")),             # $14
                _str(p.get("drive_url")),                 # $15
                _parse_published_at(p.get("published_at")),  # $16
                search_term,                              # $17
                run_id,                                   # $18
            )
            for p in posts
            if p.get("url")
        ]

        if not rows:
            return 0

        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO posts (
                    post_id, url, categoria, author, verified,
                    description, likes, comments, reposts, status,
                    error, media_url, media_type, drive_file_id, drive_url,
                    published_at, search_term, run_id
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15,
                    $16, $17, $18
                )
                ON CONFLICT (url) DO UPDATE SET
                    post_id      = COALESCE(EXCLUDED.post_id,      posts.post_id),
                    categoria    = COALESCE(EXCLUDED.categoria,    posts.categoria),
                    author       = COALESCE(EXCLUDED.author,       posts.author),
                    verified     = COALESCE(EXCLUDED.verified,     posts.verified),
                    description  = COALESCE(EXCLUDED.description,  posts.description),
                    likes        = EXCLUDED.likes,
                    comments     = EXCLUDED.comments,
                    reposts      = EXCLUDED.reposts,
                    status       = EXCLUDED.status,
                    error        = EXCLUDED.error,
                    media_url    = COALESCE(EXCLUDED.media_url, posts.media_url),
                    media_type   = COALESCE(EXCLUDED.media_type, posts.media_type),
                    drive_file_id = COALESCE(EXCLUDED.drive_file_id, posts.drive_file_id),
                    drive_url    = COALESCE(EXCLUDED.drive_url, posts.drive_url),
                    published_at = COALESCE(EXCLUDED.published_at, posts.published_at),
                    run_id       = COALESCE(EXCLUDED.run_id,       posts.run_id),
                    updated_at   = NOW()
                """,
                rows,
            )
            for post in posts:
                await self._upsert_comments(conn, post)

        return len(rows)

    async def _upsert_comments(self, conn, post: dict) -> None:
        comments = post.get("_comments") or []
        post_url = _str(post.get("url"))
        if not comments or not post_url:
            return

        post_id = _str(post.get("post_id"))
        rows = []
        for comment in comments:
            content = _str(comment.get("text") or comment.get("content"))
            if not content:
                continue
            comment_id = _str(comment.get("comment_id")) or _stable_comment_id(
                post_url,
                comment.get("author"),
                content,
                comment.get("created_at"),
            )
            rows.append(
                (
                    post_id,
                    post_url,
                    comment_id,
                    _str(comment.get("author") or comment.get("author_name")),
                    content,
                    _int(comment.get("likes")),
                    _parse_published_at(comment.get("created_at")),
                )
            )

        if not rows:
            return

        await conn.executemany(
            """
            INSERT INTO post_comments (
                post_id, post_url, comment_id,
                author, content, likes, published_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (post_url, comment_id) DO UPDATE SET
                author = COALESCE(EXCLUDED.author, post_comments.author),
                content = COALESCE(EXCLUDED.content, post_comments.content),
                likes = EXCLUDED.likes
            """,
            rows,
        )

    async def get_posts(
        self,
        search_term: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict]:
        """Recupera posts, opcionalmente filtrados por termo de busca."""
        async with self._pool.acquire() as conn:
            if search_term:
                rows = await conn.fetch(
                    """
                    SELECT * FROM posts
                    WHERE search_term = $1
                    ORDER BY collected_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    search_term,
                    limit,
                    offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM posts
                    ORDER BY collected_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit,
                    offset,
                )
        return [dict(r) for r in rows]

    async def count_posts(self, search_term: str | None = None) -> int:
        async with self._pool.acquire() as conn:
            if search_term:
                return await conn.fetchval(
                    "SELECT COUNT(*) FROM posts WHERE search_term = $1",
                    search_term,
                )
            return await conn.fetchval("SELECT COUNT(*) FROM posts")

    async def get_runs(self, limit: int = 20) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM search_runs
                ORDER BY started_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _parse_published_at(v):
    """Aceita datetime, ISO 8601 ou string 'YYYY-MM-DD HH:MM:SS' e devolve datetime.

    Retorna None se o valor estiver vazio ou em formato não reconhecido. O
    runner já normaliza para 'YYYY-MM-DD HH:MM:SS' UTC, mas tratamos formatos
    livres para robustez.
    """
    from datetime import datetime

    if not v:
        return None
    if hasattr(v, "isoformat"):
        return v
    s = str(v).strip()
    if not s:
        return None

    iso_candidate = s.replace("Z", "+00:00")
    for parser in (
        lambda x: datetime.fromisoformat(x),
        lambda x: datetime.strptime(x, "%Y-%m-%d %H:%M:%S"),
        lambda x: datetime.strptime(x, "%Y-%m-%d"),
    ):
        try:
            return parser(iso_candidate)
        except (ValueError, TypeError):
            continue
    return None


def _stable_comment_id(post_url: str, author, content: str, created_at) -> str:
    raw = f"{post_url}|{author or ''}|{content}|{created_at or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Factory: constrói o DSN a partir de variáveis individuais
# ---------------------------------------------------------------------------

def build_dsn(
    host: str,
    port: int | str,
    user: str,
    password: str,
    dbname: str,
) -> str:
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
