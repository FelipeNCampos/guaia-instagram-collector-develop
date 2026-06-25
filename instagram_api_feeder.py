"""
Instagram Feeder – Integração com GuaIA
========================================

Aguarda consultas pendentes do GuaIA, executa a coleta no Instagram
usando o InstaScraper de collector.py e devolve os resultados via
sendQuerySingleResult.

Fluxo:
  1. Conecta à API GuaIA (config.yml)
  2. Faz refresh preventivo do JWT
  3. Busca consultas pendentes a cada 30 s
  4. Para cada consulta com query_source == INSTAGRAM_QUERY_SOURCE:
       a. Marca "Em execução" (status 2)
       b. Extrai subject e max_results da consulta
       c. Executa InstaScraper.search_posts(subject, max_results)
       d. Persiste no PostgreSQL (se configurado)
       e. Envia cada post via sendQuerySingleResult
       f. Marca "Concluído" (status 3) ou "Falhou" (status 5)
  5. Aguarda 30 s e repete
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import requests
import pyGuaIA_API
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

INSTAGRAM_QUERY_SOURCE = int(
    os.getenv("INSTAGRAM_QUERY_SOURCE", "0")
)

TOKEN_REFRESH_INTERVAL = int(
    os.getenv("TOKEN_REFRESH_INTERVAL", "600")
)

QUERY_POLL_INTERVAL = int(
    os.getenv("QUERY_POLL_INTERVAL", "30")
)

FETCH_COMMENTS = os.getenv(
    "INSTAGRAM_FETCH_COMMENTS",
    os.getenv("FETCH_COMMENTS", "false"),
).lower() in ("1", "true", "yes", "on")

COMMENTS_PER_POST = int(
    os.getenv("INSTAGRAM_COMMENTS_PER_POST", os.getenv("COMMENTS_PER_POST", "50"))
)

COMMENTS_CONCURRENCY = int(
    os.getenv("INSTAGRAM_COMMENTS_CONCURRENCY", os.getenv("COMMENTS_CONCURRENCY", "3"))
)

ENV_SUFFIX = os.getenv("SUFFIX", "")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger(
    f"instagram_api_feeder{'_' + ENV_SUFFIX if ENV_SUFFIX else ''}"
)

logger.setLevel(logging.INFO)

_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s "
    "[%(funcName)s:%(lineno)d] - %(message)s",
    "%Y-%m-%d %H:%M:%S",
)

_file_handler = logging.FileHandler(
    f"instagram_api_feeder{'_' + ENV_SUFFIX if ENV_SUFFIX else ''}.log"
)

_file_handler.setFormatter(_formatter)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)

logger.addHandler(_file_handler)
logger.addHandler(_console_handler)

# ---------------------------------------------------------------------------
# JWT / RECONEXÃO
# ---------------------------------------------------------------------------

def get_guaia_connection():
    """
    Cria conexão inicial com retry.
    """
    max_retries = 5

    for attempt in range(max_retries):
        try:
            api = pyGuaIA_API.GuaIA_API("config.yml")

            logger.info(
                "Conexão inicial com API GuaIA realizada com sucesso"
            )

            return api

        except Exception as exc:
            logger.warning(
                "Erro conexão tentativa %s/%s: %s",
                attempt + 1,
                max_retries,
                exc,
            )

            if attempt < max_retries - 1:
                time.sleep(5)

            else:
                logger.exception(
                    "Falha definitiva na conexão GuaIA"
                )

                return None

    return None


def reconnect(api) -> bool:
    """
    Renova o token JWT usando connect().
    """
    try:
        ok = api.connect()

        if ok:
            logger.info("Token JWT renovado com sucesso")

        else:
            logger.warning(
                "Falha ao renovar token JWT "
                "| code=%s | reason=%s",
                api.getLastReturnCode(),
                api.getLastReturnReason(),
            )

        return ok

    except Exception as e:
        logger.error(
            "Erro ao renovar token JWT: %s",
            e,
        )

        return False


def is_auth_error(api) -> bool:
    """
    Verifica se o último erro foi de autenticação.
    """
    return api.getLastReturnCode() in (401, 403)


# ---------------------------------------------------------------------------
# WRAPPERS RESILIENTES GuaIA
# ---------------------------------------------------------------------------

def safe_get_unprocessed_queries(api):
    """
    Busca queries pendentes com reconexão automática.
    """
    try:
        ret, queries = api.getUnprocessedQueries()

        if not ret and is_auth_error(api):
            logger.warning(
                "JWT expirado ao buscar queries. "
                "Reconectando..."
            )

            if reconnect(api):
                ret, queries = api.getUnprocessedQueries()

        return ret, queries

    except ValueError as e:
        logger.warning(
            "Resposta inválida da API GuaIA: %s",
            e,
        )

        return False, []

    except requests.exceptions.ConnectionError as e:
        logger.warning(
            "Erro de conexão com GuaIA: %s",
            e,
        )

        return False, []

    except requests.exceptions.RequestException as e:
        logger.warning(
            "Erro HTTP com GuaIA: %s",
            e,
        )

        return False, []

    except Exception:
        logger.exception(
            "Erro inesperado ao buscar queries"
        )

        return False, []


def safe_set_query_status(api, id_query, status) -> bool:
    """
    Atualiza status da query com reconexão automática.
    """
    try:
        ret, _ = api.setQueryStatus(id_query, status)

        if not ret and is_auth_error(api):
            logger.warning(
                "JWT expirado ao alterar status da query %s. "
                "Reconectando...",
                id_query,
            )

            if reconnect(api):
                ret, _ = api.setQueryStatus(id_query, status)

        if not ret:
            logger.error(
                "Falha ao alterar status da query %s para %s "
                "| code=%s | reason=%s",
                id_query,
                status,
                api.getLastReturnCode(),
                api.getLastReturnReason(),
            )

        return ret

    except Exception as e:
        logger.error(
            "Erro ao alterar status da query %s para %s: %s",
            id_query,
            status,
            e,
        )

        return False


def safe_send_result(api, **kwargs) -> bool:
    """
    Envia resultado com reconexão automática.
    """
    try:
        ret, _ = api.sendQuerySingleResult(**kwargs)

        if not ret and is_auth_error(api):
            logger.warning(
                "JWT expirado durante envio de resultado. "
                "Reconectando..."
            )

            if reconnect(api):
                ret, _ = api.sendQuerySingleResult(**kwargs)

        if not ret:
            logger.error(
                "Falha ao enviar resultado "
                "| code=%s | reason=%s",
                api.getLastReturnCode(),
                api.getLastReturnReason(),
            )

        return ret

    except Exception as e:
        logger.error(
            "Erro ao enviar resultado: %s",
            e,
        )

        return False


# ---------------------------------------------------------------------------
# FILTRO DE DATA
# ---------------------------------------------------------------------------

def parse_optional_datetime(raw, *, end_of_day: bool = False):
    if raw in (None, ""):
        return None

    raw_text = raw.isoformat() if hasattr(raw, "isoformat") else str(raw)
    raw_text = raw_text.strip()

    if not raw_text:
        return None

    date_only = (
        len(raw_text) == 10
        and raw_text[4] == "-"
        and raw_text[7] == "-"
    )
    candidate = raw_text.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        logger.warning("Nao foi possivel parsear data '%s'", raw)
        return None

    if end_of_day and date_only:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    return parsed


def query_date_window(query: dict):
    return (
        parse_optional_datetime(query.get("start_date")),
        parse_optional_datetime(query.get("end_date"), end_of_day=True),
    )


def google_date_filters(start_date, end_date):
    after = None
    before = None

    if start_date:
        # Google date operators are day-based, so widen the search by one day
        # and keep the exact GuaIA range enforced by filter_posts_by_date_window.
        after = (start_date.date() - timedelta(days=1)).isoformat()

    if end_date:
        before = (end_date.date() + timedelta(days=1)).isoformat()

    return after, before


INSTAGRAM_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789-_"
)

INSTAGRAM_EPOCH_MS = 1314220021721


def instagram_shortcode_from_url(url: str) -> str:
    if not url:
        return ""

    try:
        parsed = urlparse(url)
        parts = [
            part
            for part in parsed.path.split("/")
            if part
        ]

    except Exception:
        return ""

    if len(parts) >= 2 and parts[0].lower() in ("p", "reel", "tv"):
        return parts[1]

    return ""


def instagram_media_kind_from_url(url: str) -> str:
    if not url:
        return ""

    try:
        parsed = urlparse(url)
        parts = [
            part
            for part in parsed.path.split("/")
            if part
        ]

    except Exception:
        return ""

    if len(parts) >= 2 and parts[0].lower() in ("p", "reel", "tv"):
        return parts[0].lower()

    return ""


def canonical_instagram_media_url(url: str) -> str:
    shortcode = instagram_shortcode_from_url(url)
    kind = instagram_media_kind_from_url(url)

    if not shortcode or not kind:
        return ""

    return f"https://www.instagram.com/{kind}/{shortcode}/"


def instagram_shortcode_datetime(url: str):
    shortcode = instagram_shortcode_from_url(url)

    if not shortcode:
        return None

    media_id = 0

    try:
        for char in shortcode:
            media_id = (
                media_id * 64
                + INSTAGRAM_SHORTCODE_ALPHABET.index(char)
            )

    except ValueError:
        return None

    timestamp_ms = (media_id >> 23) + INSTAGRAM_EPOCH_MS

    try:
        published_at = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.utc,
        )

    except (OverflowError, OSError, ValueError):
        return None

    if published_at.year < 2011:
        return None

    if published_at > datetime.now(timezone.utc) + timedelta(days=2):
        return None

    return published_at


def instagram_category_from_kind(kind: str) -> str:
    if kind == "reel":
        return "reel"

    if kind in ("p", "tv"):
        return "post"

    return "nao_identificado"


def fallback_post_from_url(url: str) -> dict | None:
    canonical_url = canonical_instagram_media_url(url)

    if not canonical_url:
        return None

    kind = instagram_media_kind_from_url(canonical_url)
    shortcode = instagram_shortcode_from_url(canonical_url)
    published_at = instagram_shortcode_datetime(canonical_url)

    return {
        "url": canonical_url,
        "post_id": shortcode,
        "categoria": instagram_category_from_kind(kind),
        "author": "",
        "verified": None,
        "description": "",
        "likes": 0,
        "comments": 0,
        "reposts": 0,
        "status": 1,
        "error": "fallback_search_only",
        "published_at": published_at.isoformat() if published_at else "",
        "date_filter_source": (
            "instagram_shortcode"
            if published_at
            else "fallback_search"
        ),
    }


def ensure_shortcode_publication_date(post: dict) -> bool:
    has_explicit_date = bool(
        post.get("published_at")
        or post.get("created_at")
        or post.get("data")
    )

    if has_explicit_date:
        return False

    publication_datetime = instagram_shortcode_datetime(
        post.get("url")
        or post.get("fonte")
        or ""
    )

    if not publication_datetime:
        return False

    post["published_at"] = publication_datetime.isoformat()
    post["date_filter_source"] = "instagram_shortcode"

    return True


def post_publication_datetime(post: dict):
    parsed = parse_optional_datetime(
        post.get("published_at")
        or post.get("created_at")
        or post.get("data")
    )

    if parsed:
        return parsed

    return instagram_shortcode_datetime(
        post.get("url")
        or post.get("fonte")
        or ""
    )


def filter_posts_by_date_window(
    posts: list[dict],
    start_date,
    end_date,
    *,
    allow_missing_date_from_google: bool = False,
) -> list[dict]:
    if not start_date and not end_date:
        return posts

    filtered = []
    missing_date = 0
    missing_date_kept = 0
    shortcode_date = 0
    out_of_range = 0
    fallback_date = start_date or end_date

    for post in posts:
        inferred_from_shortcode = ensure_shortcode_publication_date(post)
        publication_datetime = post_publication_datetime(post)

        if publication_datetime is None:
            missing_date += 1
            if (
                allow_missing_date_from_google
                and fallback_date
                and instagram_shortcode_from_url(
                    post.get("url")
                    or post.get("fonte")
                    or ""
                )
            ):
                post["published_at"] = fallback_date.isoformat()
                post["date_filter_source"] = "google_query_window"
                missing_date_kept += 1
                filtered.append(post)
            continue

        if inferred_from_shortcode:
            shortcode_date += 1

        if start_date and publication_datetime < start_date:
            out_of_range += 1
            continue

        if end_date and publication_datetime > end_date:
            out_of_range += 1
            continue

        filtered.append(post)

    logger.info(
        "Filtro de data aplicado | inicio=%s | fim=%s | antes=%s | depois=%s "
        "| sem_data=%s | data_shortcode=%s | sem_data_incluidos_google=%s "
        "| fora_periodo=%s",
        start_date.isoformat() if start_date else None,
        end_date.isoformat() if end_date else None,
        len(posts),
        len(filtered),
        missing_date,
        shortcode_date,
        missing_date_kept,
        out_of_range,
    )

    return filtered


# ---------------------------------------------------------------------------
# BANCO DE DADOS
# ---------------------------------------------------------------------------

async def _init_db():
    """
    Inicializa conexão PostgreSQL.
    """
    pg_host = os.getenv("POSTGRES_HOST", "")

    if not pg_host:
        return None

    try:
        from db import Database, build_dsn

        dsn = build_dsn(
            host=pg_host,
            port=os.getenv("POSTGRES_PORT", 5432),
            user=os.getenv("POSTGRES_USER", "instagram"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            dbname=os.getenv("POSTGRES_DB", "instagram_db"),
        )

        db = Database(dsn)

        await db.init()

        return db

    except Exception as exc:
        logger.warning(
            "DB indisponível (%s)",
            exc,
        )

        return None


# ---------------------------------------------------------------------------
# INSTAGRAM – COLETA
# ---------------------------------------------------------------------------

def run_brave_fallback_search(
    subject: str,
    max_results: int,
    google_after=None,
    google_before=None,
) -> list[dict]:
    """
    Fallback leve para quando Google/Selenium retorna zero links por captcha.
    A data continua sendo validada depois pelo shortcode do Instagram.
    """
    query_parts = [
        "site:instagram.com",
        f'"{subject}"',
    ]

    if google_after:
        query_parts.append(f"after:{google_after}")

    if google_before:
        query_parts.append(f"before:{google_before}")

    query = " ".join(query_parts)
    url = (
        "https://search.brave.com/search?q="
        + quote_plus(query)
    )

    logger.info(
        "Fallback Brave Search iniciado | subject='%s' | after=%s | before=%s",
        subject,
        google_after,
        google_before,
    )

    try:
        from bs4 import BeautifulSoup

        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            },
            timeout=20,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

    except Exception:
        logger.exception("Falha no fallback Brave Search")
        return []

    posts = []
    seen = set()

    for anchor in soup.select("a[href]"):
        post = fallback_post_from_url(anchor.get("href", ""))

        if not post:
            continue

        url_key = post["url"]

        if url_key in seen:
            continue

        seen.add(url_key)
        posts.append(post)

        if len(posts) >= max_results:
            break

    logger.info(
        "Fallback Brave Search concluido | encontrados=%s",
        len(posts),
    )

    return posts


async def run_instagram_search(
    subject: str,
    max_results: int,
    start_date=None,
    end_date=None,
    id_query=None,
) -> list[dict]:
    """
    Executa o InstaScraper com o termo recebido do GuaIA.
    """
    if not subject or not subject.strip():
        logger.error(
            "run_instagram_search chamado sem termo de busca"
        )

        return []

    from collector import InstaScraper

    headless = os.getenv(
        "HEADLESS",
        "true",
    ).lower() not in ("0", "false", "no")

    db = await _init_db()

    run_id = None

    if db:
        try:
            run_id = await db.create_run(
                subject,
                max_results,
            )

        except Exception:
            logger.exception(
                "Falha ao registrar run no banco"
            )

            run_id = None

    google_after, google_before = google_date_filters(start_date, end_date)

    logger.info(
        "Filtros Google Instagram | subject='%s' | after=%s | before=%s",
        subject,
        google_after,
        google_before,
    )

    scraper = InstaScraper(
        headless=headless,
        search_term=subject,
        after=google_after,
        before=google_before,
    )

    try:
        urls = await scraper.search_posts(
            subject,
            target=max_results,
        )

    except Exception:
        logger.exception(
            "Falha ao executar coleta no Instagram"
        )

        if db:
            try:
                if run_id is not None:
                    await db.finish_run(run_id, 0)

            finally:
                await db.close()

        return []

    posts = [
        scraper.posts.get(url, {"url": url})
        for url in urls
    ]

    if not posts:
        posts = await asyncio.to_thread(
            run_brave_fallback_search,
            subject,
            max_results,
            google_after,
            google_before,
        )

    for post in posts:
        ensure_shortcode_publication_date(post)

    if FETCH_COMMENTS:
        try:
            await scraper.fetch_comments_all(
                posts,
                max_per_post=COMMENTS_PER_POST,
                concurrency=COMMENTS_CONCURRENCY,
            )
        except Exception:
            logger.exception("Falha best-effort ao coletar comentarios Instagram")

    try:
        from media_archiver import archive_posts_media

        posts = await asyncio.to_thread(
            archive_posts_media,
            posts,
            search_term=subject,
            id_query=id_query,
            logger=logger,
        )
    except Exception:
        logger.exception("Falha best-effort ao arquivar midias Instagram")

    if db and posts:
        try:
            saved = await db.upsert_posts(
                posts,
                subject,
                run_id,
            )

            logger.info(
                "PostgreSQL: %d posts salvos",
                saved,
            )

        except Exception:
            logger.exception(
                "Erro ao persistir posts no banco"
            )

        finally:
            try:
                if run_id is not None:
                    await db.finish_run(
                        run_id,
                        len(posts),
                    )

            except Exception:
                logger.exception(
                    "Erro ao finalizar run no banco"
                )

            await db.close()

    return posts


# ---------------------------------------------------------------------------
# DTO PARA O GuaIA
# ---------------------------------------------------------------------------

def derive_primary_source_name(
    post: dict,
    query_subject: str = "",
) -> str:
    """
    GuaIA exige primary_source_name não vazio.
    """
    raw = (post.get("author") or "").strip()

    if raw:
        return raw

    url = (post.get("url") or "").strip()

    try:
        parsed = urlparse(url)

        host = (parsed.netloc or "").lower()

        parts = [
            p
            for p in parsed.path.split("/")
            if p
        ]

        if (
            "help.instagram.com" in host
            or "about.instagram.com" in host
        ):
            return "Instagram (página de ajuda)"

        if parts:
            head = parts[0].lower()

            if head in (
                "accounts",
                "explore",
                "stories",
                "legal",
            ):
                return "Instagram (navegação)"

            if head == "p" and len(parts) >= 2:
                return f"Instagram:{parts[1]}"

            if head == "reel" and len(parts) >= 2:
                return f"Instagram:{parts[1]}"

            if head == "tv" and len(parts) >= 2:
                return f"Instagram:{parts[1]}"

            if head == "reels":
                if len(parts) >= 2:
                    return f"@{parts[1]}"

                return "Instagram (Reels)"

            return f"@{parts[0]}"

    except Exception:
        pass

    subj = (query_subject or "").strip()

    if subj:
        return f"Instagram (busca: {subj})"

    return "Instagram (autor desconhecido)"


def create_instagram_dto(post: dict) -> dict:
    """
    Constrói DTO do post Instagram.
    """
    comment_list = []
    for comment in post.get("_comments") or []:
        comment_list.append(
            {
                "author": comment.get("author") or comment.get("author_name") or "",
                "text": comment.get("text") or comment.get("content") or "",
                "score": comment.get("likes", 0),
                "created_utc": comment.get("created_at"),
            }
        )

    return {
        "post_id": post.get("post_id"),
        "categoria": post.get("categoria"),
        "author": post.get("author"),
        "verified": post.get("verified"),
        "description": post.get("description"),
        "likes": post.get("likes", 0),
        "comment_count": post.get("comments", 0),
        "reposts": post.get("reposts", 0),
        "status": post.get("status", 0),
        "error": post.get("error", ""),
        "media_url": post.get("media_url", ""),
        "media_type": post.get("media_type", ""),
        "drive_file_id": post.get("drive_file_id", ""),
        "drive_url": post.get("drive_url", ""),
        "comments": comment_list,
    }


# ---------------------------------------------------------------------------
# PROCESSAMENTO DE QUERY
# ---------------------------------------------------------------------------

def process_query(api, query: dict):
    """
    Processa uma query individual do GuaIA.
    """
    id_query = query.get("id_query")

    logger.info(
        "Processando consulta Instagram ID (GuaIA): %s",
        id_query,
    )

    if not safe_set_query_status(api, id_query, 2):
        return

    subject = (query.get("subject") or "").strip()

    if not subject:
        logger.error(
            "Consulta %s sem termo de busca",
            id_query,
        )

        safe_set_query_status(api, id_query, 5)

        return

    max_results = query.get("max_results", 200)
    start_date, end_date = query_date_window(query)

    logger.info(
        "Termo recebido do GuaIA: '%s' | max_results=%s | start_date=%s | end_date=%s",
        subject,
        max_results,
        start_date.isoformat() if start_date else None,
        end_date.isoformat() if end_date else None,
    )

    try:
        posts = asyncio.run(
            run_instagram_search(
                subject,
                max_results,
                start_date,
                end_date,
                id_query=id_query,
            )
        )

    except Exception:
        logger.exception(
            "Falha ao executar busca Instagram "
            "da consulta %s",
            id_query,
        )

        safe_set_query_status(api, id_query, 5)

        return

    posts = filter_posts_by_date_window(
        posts,
        start_date,
        end_date,
        allow_missing_date_from_google=bool(start_date or end_date),
    )

    if not posts:
        logger.warning(
            "Nenhum post coletado dentro do periodo para a consulta %s",
            id_query,
        )

        safe_set_query_status(api, id_query, 4)

        return

    logger.info(
        "Encontrados %s posts para a consulta %s",
        len(posts),
        id_query,
    )

    enviados = 0
    falhas = 0

    for post in posts:
        try:
            primary_name = derive_primary_source_name(
                post,
                subject,
            )

            post_for_dto = {
                **post,
                "author": (
                    (post.get("author") or "").strip()
                    or primary_name
                ),
            }

            insta_dto = create_instagram_dto(
                post_for_dto
            )

            insta_dto["busca_id_id"] = str(id_query)

            url = post.get("url", "")

            content = post.get("description", "")

            raw_date = post.get("published_at", "")

            try:
                pub_date = (
                    datetime.fromisoformat(
                        raw_date.replace("Z", "+00:00")
                    )
                    if raw_date
                    else datetime.now(
                        tz=timezone.utc
                    )
                )

            except Exception:
                pub_date = datetime.now(
                    tz=timezone.utc
                )

            ok = safe_send_result(
                api,
                id_query=id_query,
                publication_date=pub_date,
                publication_text=content,
                source_type=INSTAGRAM_QUERY_SOURCE,
                other_source_name="Instagram",
                primary_source_name=primary_name,
                publication_url=url,
                allegation="",
                comment="",
                observation="",
                id_target_type="1,5",
                social_metadata=insta_dto,
            )

            if ok:
                enviados += 1
            else:
                falhas += 1

        except Exception:
            logger.exception(
                "Erro ao processar post %s",
                post.get("url"),
            )

            falhas += 1

    logger.info(
        "Envio concluído | query=%s | enviados=%s | falhas=%s",
        id_query,
        enviados,
        falhas,
    )

    if falhas == 0:
        safe_set_query_status(api, id_query, 3)

        logger.info(
            "Consulta %s concluída com sucesso",
            id_query,
        )

    else:
        safe_set_query_status(api, id_query, 4)

        logger.warning(
            "Consulta %s concluída com falhas",
            id_query,
        )


# ---------------------------------------------------------------------------
# LOOP PRINCIPAL
# ---------------------------------------------------------------------------

def main():
    if not INSTAGRAM_QUERY_SOURCE:
        logger.error(
            "INSTAGRAM_QUERY_SOURCE não definido. "
            "Configure no .env o ID do Instagram."
        )

        sys.exit(1)

    logger.info(
        "Iniciando integração do Instagram com API GuaIA"
    )

    api = get_guaia_connection()

    if not api:
        sys.exit(1)

    last_token_refresh = time.time()

    while True:
        try:
            # ---------------------------------------------------------------
            # REFRESH PREVENTIVO JWT
            # ---------------------------------------------------------------

            if (
                time.time() - last_token_refresh
                >= TOKEN_REFRESH_INTERVAL
            ):
                logger.info(
                    "Renovando JWT preventivamente..."
                )

                reconnect(api)

                last_token_refresh = time.time()

            # ---------------------------------------------------------------
            # BUSCA QUERIES
            # ---------------------------------------------------------------

            logger.info(
                "Buscando consultas não processadas..."
            )

            ret, queries = safe_get_unprocessed_queries(api)

            if not ret:
                logger.warning(
                    "Falha ao buscar queries pendentes"
                )

                time.sleep(QUERY_POLL_INTERVAL)

                continue

            if not queries:
                logger.info(
                    "Nenhuma consulta pendente encontrada"
                )

                time.sleep(QUERY_POLL_INTERVAL)

                continue

            logger.info(
                "Total de consultas pendentes: %s",
                len(queries),
            )

            # ---------------------------------------------------------------
            # PROCESSA QUERIES
            # ---------------------------------------------------------------

            for query in queries:

                query_source = query.get("query_source")

                try:
                    if (
                        int(query_source)
                        != INSTAGRAM_QUERY_SOURCE
                    ):
                        continue

                except (TypeError, ValueError):
                    logger.warning(
                        "query_source inválido na query %s: %s",
                        query.get("id_query"),
                        query_source,
                    )

                    continue

                try:
                    process_query(api, query)

                except requests.exceptions.ConnectionError as e:
                    logger.error(
                        "Erro de rede processando query %s: %s",
                        query.get("id_query"),
                        e,
                    )

                    safe_set_query_status(
                        api,
                        query.get("id_query"),
                        5,
                    )

                    time.sleep(60)

                except Exception:
                    logger.exception(
                        "Erro processando query %s",
                        query.get("id_query"),
                    )

                    safe_set_query_status(
                        api,
                        query.get("id_query"),
                        5,
                    )

            time.sleep(QUERY_POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info(
                "Encerrando integração Instagram → GuaIA"
            )

            break

        except Exception:
            logger.exception(
                "Erro geral no loop principal"
            )

            time.sleep(30)


if __name__ == "__main__":
    main()
