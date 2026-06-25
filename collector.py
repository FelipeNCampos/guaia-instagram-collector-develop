import asyncio
import http.cookiejar
import json
import os
import tempfile
import re
import threading
import time
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, quote_plus, urlparse
from xml.sax.saxutils import escape

import dotenv
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchWindowException,
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

APP_NAME = "instagram_collector"

# Persistência interna (cookies, perfil do Chrome, checkpoints).
# Estrutura espelha o padrão do kwai_collector: tudo em ./data ao lado do código.
APP_ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_ROOT_DIR / ".env"
INTERNAL_DIR = APP_ROOT_DIR / "data"
INTERNAL_DIR.mkdir(exist_ok=True)
DEFAULT_CHROME_PROFILE = INTERNAL_DIR / "chrome_profile"
OUTPUT_ROOT_DIR = APP_ROOT_DIR / "output"
OUTPUT_SUBDIR_NAME = "insta"
RUNTIME_PROFILES_DIR = OUTPUT_ROOT_DIR / "_runtime_profiles"

dotenv.load_dotenv(ENV_FILE)

INSTAGRAM_CATEGORY_ORDER = [
    "perfil",
    "post",
    "reel",
    "nao_identificado",
]
INSTAGRAM_DEFAULT_HEADERS = ["fonte", "status", "error", "data"]
INSTAGRAM_POST_HEADERS = [
    *INSTAGRAM_DEFAULT_HEADERS,
    "perfil_publicador",
    "verificado",
    "descricao_post",
    "numero_likes",
    "numero_comentarios",
    "numero_reposts",
    "media_url",
    "media_type",
    "drive_file_id",
    "drive_url",
]
INSTAGRAM_REEL_HEADERS = [
    *INSTAGRAM_DEFAULT_HEADERS,
    "perfil_publicador",
    "verificado",
    "descricao_reel",
    "numero_likes",
    "numero_comentarios",
    "numero_reposts",
    "media_url",
    "media_type",
    "drive_file_id",
    "drive_url",
]
INSTAGRAM_POST_DATA_DEFAULTS = {
    "perfil_publicador": "",
    "verificado": "",
    "descricao_post": "",
    "numero_likes": "",
    "numero_comentarios": "",
    "numero_reposts": "",
    "media_url": "",
    "media_type": "",
    "drive_file_id": "",
    "drive_url": "",
    "data": "",
}
INSTAGRAM_REEL_DATA_DEFAULTS = {
    "perfil_publicador": "",
    "verificado": "",
    "descricao_reel": "",
    "numero_likes": "",
    "numero_comentarios": "",
    "numero_reposts": "",
    "media_url": "",
    "media_type": "",
    "drive_file_id": "",
    "drive_url": "",
    "data": "",
}
INSTAGRAM_METRIC_EXTRACTION_RULES = {
    "numero_likes": {
        "keywords": ("like", "likes", "curtida", "curtidas", "curtido por", "curtiram"),
        "extra_patterns": [
            r"(?:curtido por|liked by).*?(?:outras?|others?|and)\s+(?P<count>\d+(?:[.,]\d+)?)\s*(?P<suffix>mil|mi|bi|k|m|b)?\b",
        ],
    },
    "numero_comentarios": {
        "keywords": ("comment", "comments", "comentario", "comentarios"),
        "extra_patterns": [],
    },
    "numero_reposts": {
        "keywords": ("share", "shares", "repost", "reposts", "compartilhar", "compartilhamentos"),
        "extra_patterns": [],
    },
}
INSTAGRAM_PROFILE_RESERVED_PATHS = {
    "about",
    "accounts",
    "api",
    "challenge",
    "developer",
    "developers",
    "direct",
    "download",
    "emails",
    "explore",
    "graphql",
    "help",
    "legal",
    "oauth",
    "p",
    "press",
    "privacy",
    "reel",
    "reels",
    "session",
    "share",
    "stories",
    "terms",
    "tv",
    "web",
}
INSTAGRAM_LOGIN_POPUP_CLOSE_XPATHS = [
    "/html/body/div[1]/div/div/div[2]/div/div/div[1]/div[1]/div[2]/section/main/div[1]/div[2]/div/div/div/div/div[1]/div",
    "//div[@role='dialog']//*[self::button or @role='button'][@aria-label='Fechar' or @aria-label='Close']",
    "//div[@role='dialog']//*[self::button or @role='button'][.//*[local-name()='svg' and (@aria-label='Fechar' or @aria-label='Close')]]",
    "//div[@role='dialog']//*[self::button or @role='button'][.//*[local-name()='title' and (normalize-space()='Fechar' or normalize-space()='Close')]]",
    "//div[@role='dialog']//*[self::button or @role='button'][normalize-space()='Agora nao' or normalize-space()='Agora não' or normalize-space()='Not now']",
]
INSTAGRAM_LOGIN_POPUP_TEXT_MARKERS = [
    "continue as",
    "entre para ver o conteudo",
    "entre para ver o conteúdo",
    "entrar para curtir ou comentar",
    "nao perca nenhum post",
    "não perca nenhum post",
    "cadastre-se no instagram",
    "log in to instagram",
    "see instagram photos and videos",
    "sign up to see photos and videos from your friends",
    "veja fotos e videos do instagram",
    "veja fotos e vídeos do instagram",
]


def log_instagram(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[instagram {timestamp}] {message}", flush=True)


def format_progress(current, total):
    if total <= 0:
        return str(current)

    percentage = int((current / total) * 100)
    return f"{current}/{total} ({percentage}%)"


def summarize_record_counts(records):
    counts = {category: 0 for category in INSTAGRAM_CATEGORY_ORDER}

    for record in records:
        counts[record.get("categoria", "nao_identificado")] = (
            counts.get(record.get("categoria", "nao_identificado"), 0) + 1
        )

    ordered_categories = INSTAGRAM_CATEGORY_ORDER + [
        category for category in counts if category not in INSTAGRAM_CATEGORY_ORDER
    ]
    return ", ".join(
        f"{category}={counts[category]}"
        for category in ordered_categories
        if counts.get(category, 0) > 0
    ) or "sem_resultados"


def get_chrome_profile_path():
    profile_path = os.getenv(
        "INSTAGRAM_CHROME_PROFILE_DIRECTORY",
        str(DEFAULT_CHROME_PROFILE),
    )
    profile_path = Path(profile_path)

    if not profile_path.is_absolute():
        profile_path = ENV_FILE.parent / profile_path

    profile_path.mkdir(parents=True, exist_ok=True)
    return profile_path


def get_runtime_profile_path():
    if _chrome_in_docker() and not _docker_allow_chrome_profile_on_app_volume():
        session = Path(
            tempfile.mkdtemp(prefix="insta_run_", dir=str(_docker_chrome_session_root()))
        )
        log_instagram(
            "Docker: perfil de sessao em /tmp (Chrome falha com user-data-dir em volume "
            f"montado do host). {session}"
        )
        return session
    return get_chrome_profile_path()


def get_instagram_post_worker_count():
    try:
        configured_workers = int(os.getenv("INSTAGRAM_POST_WORKERS", "4"))
    except ValueError:
        configured_workers = 4

    return max(1, configured_workers)


def get_instagram_post_checkpoint_interval():
    try:
        configured_interval = int(os.getenv("INSTAGRAM_POST_CHECKPOINT_INTERVAL", "10"))
    except ValueError:
        configured_interval = 10

    return max(1, configured_interval)


def should_block_images_for_instagram_driver():
    return os.getenv("INSTAGRAM_BLOCK_IMAGES", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _chrome_in_docker() -> bool:
    """True no container Docker / Kubernetes (Chrome precisa de flags extra)."""
    v = os.getenv("CHROME_IN_DOCKER", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return os.path.exists("/.dockerenv")


def _docker_allow_chrome_profile_on_app_volume() -> bool:
    """Linux com volume nativo em /app/data pode definir INSTAGRAM_ALLOW_DATA_VOLUME_PROFILE=1."""
    return os.getenv("INSTAGRAM_ALLOW_DATA_VOLUME_PROFILE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _chrome_user_data_dir_is_docker_bind_mount(profile_path: Path) -> bool:
    """Docker Desktop: user-data-dir em ./data ou ./output montados do Windows derruba o Chrome na hora."""
    if not _chrome_in_docker() or _docker_allow_chrome_profile_on_app_volume():
        return False
    try:
        resolved = profile_path.resolve()
    except OSError:
        resolved = profile_path.expanduser()
    s = str(resolved).replace("\\", "/")
    return s.startswith("/app/data") or s.startswith("/app/output")


def _docker_chrome_session_root() -> Path:
    root = Path(os.getenv("INSTAGRAM_DOCKER_CHROME_ROOT", "/tmp/instagram_chrome_sessions"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_google_cookies_file():
    """Arquivo Netscape cookies.txt (ex.: extensão 'Get cookies.txt LOCALLY')."""
    candidates = []
    env = os.getenv("INSTAGRAM_GOOGLE_COOKIES_FILE", "").strip()
    if env:
        p = Path(env)
        if not p.is_absolute():
            p = APP_ROOT_DIR / p
        candidates.append(p)
    candidates.extend(
        [
            INTERNAL_DIR / "www.google.com_cookies.txt",
            APP_ROOT_DIR / "www.google.com_cookies.txt",
            INTERNAL_DIR / "google_cookies.txt",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _google_cookies_output_file() -> Path:
    return INTERNAL_DIR / "www.google.com_cookies.txt"


def _save_google_netscape_cookies(driver, cookies_path: Path | None = None) -> int:
    cookies_path = cookies_path or _google_cookies_output_file()

    try:
        cookies = driver.get_cookies()
    except WebDriverException:
        return 0

    google_cookies = []

    for cookie in cookies:
        domain = (cookie.get("domain") or "").lower()
        name = cookie.get("name")
        value = cookie.get("value")

        if "google." not in domain or not name or value is None:
            continue

        google_cookies.append(cookie)

    if not google_cookies:
        return 0

    try:
        cookies_path.parent.mkdir(parents=True, exist_ok=True)

        with cookies_path.open("w", encoding="utf-8") as fp:
            fp.write("# Netscape HTTP Cookie File\n")
            fp.write("# Generated by instagram_collector after a valid Google SERP.\n")

            for cookie in google_cookies:
                domain = cookie.get("domain") or ".google.com"
                include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
                path = cookie.get("path") or "/"
                secure = "TRUE" if cookie.get("secure") else "FALSE"
                expiry = int(cookie.get("expiry") or 0)
                name = cookie.get("name")
                value = cookie.get("value")

                fp.write(
                    f"{domain}\t{include_subdomains}\t{path}\t{secure}\t"
                    f"{expiry}\t{name}\t{value}\n"
                )
    except OSError:
        return 0

    log_instagram(
        f"Cookies Google salvos: {len(google_cookies)} ({cookies_path.name})"
    )

    return len(google_cookies)


def _inject_google_netscape_cookies(driver, cookies_path: Path) -> int:
    """Abre google.com e aplica cookies do export Netscape; reduz páginas de verificação."""
    driver.get("https://www.google.com/")
    time.sleep(1)

    jar = http.cookiejar.MozillaCookieJar(str(cookies_path))
    jar.load(ignore_discard=True, ignore_expires=True)
    rows = list(jar)
    added = 0

    for cookie in rows:
        entry = {
            "name": cookie.name,
            "value": cookie.value,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
        }
        if cookie.domain:
            entry["domain"] = cookie.domain
        if cookie.expires is not None:
            try:
                exp = int(cookie.expires)
                if exp > 0:
                    entry["expiry"] = exp
            except (TypeError, ValueError):
                pass

        try:
            driver.add_cookie(entry)
            added += 1
        except WebDriverException:
            try:
                minimal = {
                    "name": cookie.name,
                    "value": cookie.value,
                    "path": cookie.path or "/",
                    "secure": bool(cookie.secure),
                }
                if cookie.expires is not None:
                    try:
                        exp = int(cookie.expires)
                        if exp > 0:
                            minimal["expiry"] = exp
                    except (TypeError, ValueError):
                        pass
                driver.add_cookie(minimal)
                added += 1
            except WebDriverException:
                pass

    log_instagram(
        f"Cookies Google (Netscape): {added}/{len(rows)} aceitos pelo Chrome "
        f"({cookies_path.name})"
    )
    driver.refresh()
    time.sleep(1)
    return added


def get_twocaptcha_api_key():
    return os.getenv("TWOCAPTCHA_API_KEY", "").strip() or os.getenv(
        "2CAPTCHA_API_KEY", ""
    ).strip()


def should_inject_google_netscape_cookies():
    """Com TWOCAPTCHA_API_KEY, deixa de usar cookies Netscape do Google, salvo override explícito."""
    if not get_twocaptcha_api_key():
        return True
    if os.getenv("INSTAGRAM_USE_GOOGLE_COOKIES_FILE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    return _resolve_google_cookies_file() is not None


def build_twocaptcha_solver():
    key = get_twocaptcha_api_key()
    if not key:
        return None
    try:
        from twocaptcha import TwoCaptcha
    except ImportError:
        log_instagram("2Captcha: pacote ausente. Instale com: pip install 2captcha-python")
        return None
    try:
        recaptcha_timeout = int(os.getenv("TWOCAPTCHA_RECAPTCHA_TIMEOUT", "600"))
    except ValueError:
        recaptcha_timeout = 600
    return TwoCaptcha(key, recaptchaTimeout=max(120, recaptcha_timeout))


def _twocaptcha_extract_token(result):
    if result is None:
        return None
    if isinstance(result, dict):
        code = result.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
        return None
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def _extract_hcaptcha_sitekey(page_source):
    if "hcaptcha.com" not in page_source.lower() and "h-captcha" not in page_source.lower():
        return None
    m = re.search(
        r'data-sitekey=["\']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})["\']',
        page_source,
        flags=re.I,
    )
    if m:
        return m.group(1)
    m = re.search(r'data-sitekey=["\']([^"\']{10,})["\']', page_source, flags=re.I)
    return m.group(1) if m else None


def _extract_recaptcha_sitekey(page_source):
    for pat in (
        r'data-sitekey=["\']([0-9A-Za-z_-]{20,})["\']',
        r'"sitekey"\s*:\s*["\']([0-9A-Za-z_-]{20,})["\']',
    ):
        m = re.search(pat, page_source)
        if m:
            return m.group(1)
    return None


def _extract_recaptcha_sitekey_from_urls_in_html(html):
    """Sitekey em URLs embutidas no HTML (ex.: src do iframe no DOM serializado)."""
    for m in re.finditer(
        r"https?://[^\s\"'<>]*?recaptcha[^\s\"'<>]*?[?&]k=([0-9A-Za-z_-]{20,})",
        html or "",
        flags=re.I,
    ):
        return m.group(1)
    return None


def _extract_recaptcha_sitekey_from_iframe_srcs(driver):
    """Página principal muitas vezes não inclui o widget; o sitekey vem no query param `k=` do iframe."""
    selectors = (
        "iframe[src*='recaptcha']",
        "iframe[src*='google.com/recaptcha']",
        "iframe[title*='reCAPTCHA' i]",
    )
    seen = set()
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                src = (el.get_attribute("src") or "").strip()
                if not src or src in seen:
                    continue
                seen.add(src)
                parsed = urlparse(src)
                qs = parse_qs(parsed.query)
                for param in ("k", "sitekey"):
                    for val in qs.get(param) or ():
                        val = (val or "").strip()
                        if len(val) >= 20 and re.match(r"^[0-9A-Za-z_-]+$", val):
                            return val
        except Exception:
            continue
    return None


def _recaptcha_html_blob(driver, page_source):
    """Junta HTML principal + URLs dos iframes reCAPTCHA (para enterprise / sitekey)."""
    parts = [page_source or ""]
    try:
        for el in driver.find_elements(
            By.CSS_SELECTOR,
            "iframe[src*='recaptcha'], iframe[src*='google.com/recaptcha']",
        ):
            src = (el.get_attribute("src") or "").strip()
            if src:
                parts.append(src)
    except Exception:
        pass
    return "\n".join(parts)


def _extract_recaptcha_data_s(page_source):
    m = re.search(r'data-s=["\']([^"\']+)["\']', page_source, flags=re.I)
    if not m:
        return None
    s = m.group(1).strip()
    return s if len(s) > 5 else None


def _detect_recaptcha_enterprise(page_source):
    return bool(
        re.search(r"grecaptcha\.enterprise|recaptcha/enterprise", page_source, flags=re.I)
    )


def _detect_recaptcha_invisible(page_source):
    if re.search(r'data-size=["\']invisible["\']', page_source, flags=re.I):
        return True
    return "invisible recaptcha" in page_source.lower()


def _twocaptcha_inject_recaptcha_token(driver, token):
    driver.execute_script(
        """
        const t = arguments[0];
        const el = document.querySelector(
          '#g-recaptcha-response, textarea[name="g-recaptcha-response"]'
        );
        if (el) {
          el.value = t;
          el.innerHTML = t;
        }
        const withCb = document.querySelector('.g-recaptcha[data-callback]');
        if (withCb && withCb.dataset.callback
            && typeof window[withCb.dataset.callback] === 'function') {
          window[withCb.dataset.callback](t);
        }
        if (typeof ___grecaptcha_cfg !== 'undefined') {
          try {
            const clients = ___grecaptcha_cfg.clients || {};
            for (const cId of Object.keys(clients)) {
              const client = clients[cId];
              for (const k of Object.keys(client || {})) {
                const item = client[k];
                if (item && typeof item.callback === 'function') {
                  try { item.callback(t); } catch (e) {}
                }
              }
            }
          } catch (e) {}
        }
        """,
        token,
    )


def _twocaptcha_inject_hcaptcha_token(driver, token):
    driver.execute_script(
        """
        const t = arguments[0];
        const el = document.querySelector(
          'textarea[name="h-captcha-response"], textarea.h-captcha-response'
        );
        if (el) {
          el.value = t;
          el.innerHTML = t;
        }
        """,
        token,
    )


def _twocaptcha_submit_captcha_context(driver):
    driver.execute_script(
        """
        const ta = document.querySelector(
          '[name="g-recaptcha-response"], [name="h-captcha-response"]'
        );
        if (ta) {
          const f = ta.closest('form');
          if (f) {
            if (typeof f.requestSubmit === 'function') {
              f.requestSubmit();
              return true;
            }
            f.submit();
            return true;
          }
        }
        const submit = document.querySelector(
          'form button[type="submit"], form input[type="submit"], '
          + 'input[name="btnK"], button[name="btnK"]'
        );
        if (submit) {
          submit.click();
          return true;
        }
        return false;
        """
    )


def try_twocaptcha_solve_current_page(driver):
    """Resolve reCAPTCHA/hCaptcha na URL atual via 2Captcha e injeta o token."""
    solver = build_twocaptcha_solver()
    if solver is None:
        return False

    try:
        from twocaptcha.exceptions.solver import (
            ApiException,
            NetworkException,
            TimeoutException,
            ValidationException,
        )
    except ImportError:
        return False

    try:
        page_url = get_driver_current_url(driver)
        page_source = get_driver_page_source(driver)
        if not page_source or len(page_source) < 200:
            return False

        user_agent = ""
        try:
            user_agent = driver.execute_script("return navigator.userAgent || ''") or ""
        except Exception:
            pass

        blob = _recaptcha_html_blob(driver, page_source)

        hc_key = _extract_hcaptcha_sitekey(page_source)
        if hc_key:
            log_instagram(f"2Captcha: resolvendo hCaptcha (sitekey …{hc_key[-10:]}).")
            try:
                if user_agent:
                    res = solver.hcaptcha(hc_key, page_url, userAgent=user_agent)
                else:
                    res = solver.hcaptcha(hc_key, page_url)
            except TypeError:
                res = solver.hcaptcha(hc_key, page_url)
            token = _twocaptcha_extract_token(res)
            if not token:
                log_instagram("2Captcha: resposta hCaptcha sem token.")
                return False
            _twocaptcha_inject_hcaptcha_token(driver, token)
            _twocaptcha_submit_captcha_context(driver)
            log_instagram("2Captcha: token hCaptcha aplicado.")
            return True

        rc_key = (
            _extract_recaptcha_sitekey(page_source)
            or _extract_recaptcha_sitekey(blob)
            or _extract_recaptcha_sitekey_from_urls_in_html(page_source)
            or _extract_recaptcha_sitekey_from_urls_in_html(blob)
            or _extract_recaptcha_sitekey_from_iframe_srcs(driver)
        )
        if not rc_key:
            log_instagram(
                "2Captcha: nenhum sitekey reCAPTCHA encontrado (pagina principal nem iframes). "
                f"url={page_url[:120]}…"
            )
            return False

        data_s = _extract_recaptcha_data_s(page_source)
        enterprise = 1 if _detect_recaptcha_enterprise(blob) else 0
        invisible = 1 if _detect_recaptcha_invisible(blob) else 0

        log_instagram(
            f"2Captcha: resolvendo reCAPTCHA (sitekey …{rc_key[-8:]}, "
            f"enterprise={enterprise}, invisible={invisible})."
        )

        kwargs = {}
        if user_agent:
            kwargs["userAgent"] = user_agent
        if data_s:
            kwargs["data-s"] = data_s

        token = None
        enterprises = [1] if enterprise else [0, 1]
        for ent in enterprises:
            try:
                res = solver.recaptcha(
                    rc_key,
                    page_url,
                    version="v2",
                    enterprise=ent,
                    invisible=invisible,
                    **kwargs,
                )
                token = _twocaptcha_extract_token(res)
                if token:
                    break
            except (ApiException, ValidationException) as exc:
                log_instagram(f"2Captcha: reCAPTCHA enterprise={ent} recusada: {exc}")

        if not token:
            log_instagram("2Captcha: não obteve token reCAPTCHA.")
            return False

        _twocaptcha_inject_recaptcha_token(driver, token)
        _twocaptcha_submit_captcha_context(driver)
        log_instagram("2Captcha: token reCAPTCHA aplicado.")
        return True

    except TimeoutException as exc:
        log_instagram(f"2Captcha: timeout ({exc}).")
    except (ApiException, NetworkException, ValidationException) as exc:
        log_instagram(f"2Captcha: erro ({exc}).")
    except Exception as exc:
        log_instagram(f"2Captcha: falha ({exc}).")

    return False


def _google_page_shows_captcha_widget(driver):
    if _driver_on_google_sorry_page(driver):
        return True
    if get_visible_instagram_captcha_marker(driver):
        return True
    ps = get_driver_page_source(driver).lower()
    if _extract_recaptcha_sitekey_from_iframe_srcs(driver):
        return True
    if "data-sitekey=" not in ps:
        return False
    return any(
        x in ps
        for x in (
            "g-recaptcha",
            "hcaptcha",
            "h-captcha",
            "recaptcha/api.js",
            "recaptcha/enterprise",
        )
    )


def create_driver(headless=True, profile_path=None):
    """Selenium 4+ Docker Grid - versão FINAL."""
    
    if _chrome_in_docker():
        headless = True
        log_instagram("Docker: Selenium 4+ modo headless")
    
    initial_profile = Path(profile_path) if profile_path else get_chrome_profile_path()
    initial_profile.mkdir(parents=True, exist_ok=True)

    chrome_profile_path = initial_profile
    if _chrome_user_data_dir_is_docker_bind_mount(initial_profile):
        chrome_profile_path = Path(
            tempfile.mkdtemp(prefix="insta_ud_", dir=str(_docker_chrome_session_root()))
        )
        log_instagram(
            "Docker: user-data-dir em volume montado redirecionado para "
            f"{chrome_profile_path}"
        )

    # Selenium 4+ usa APENAS options
    options = Options()
    
    # Flags Chrome
    options.page_load_strategy = "eager"
    
    flags = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-images",
        "--window-size=1920,1080",
        "--disable-logging",
        "--log-level=3",
        "--disable-popup-blocking",
        "--disable-notifications",
        "--disable-background-timer-throttling",
    ]
    
    if profile_path or (
        _chrome_in_docker() and chrome_profile_path is not initial_profile
    ):
        options.add_argument(f"--user-data-dir={chrome_profile_path}")
    
    if headless:
        options.add_argument("--headless=new")
    
    for flag in flags:
        options.add_argument(flag)
    
    # Anti-detecção
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    # Prefs
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values": {
            "images": 2,
            "notifications": 2,
            "geolocation": 2
        }
    })
    
    try:
        # Selenium 4+ SEM desired_capabilities
        driver = webdriver.Chrome(options=options)
        
        # Remove webdriver property
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        cookies_path = _resolve_google_cookies_file()
        if cookies_path is not None and should_inject_google_netscape_cookies():
            try:
                _inject_google_netscape_cookies(driver, cookies_path)
            except Exception as exc:
                log_instagram(
                    f"Aviso: nao foi possivel aplicar cookies Google ({cookies_path.name}): {exc}"
                )
                driver.get("about:blank")
        else:
            driver.get("about:blank")

        log_instagram("Selenium 4+ Chrome OK!")
        return driver
        
    except Exception as e:
        log_instagram(f"Chrome falhou: {e}")
        raise RuntimeError(f"Chrome falhou: {e}")

def is_webdriver_window_lost_error(exception):
    error_text = clean_text(exception).lower()
    return isinstance(exception, NoSuchWindowException) or any(
        marker in error_text
        for marker in (
            "no such window",
            "target window already closed",
            "web view not found",
            "invalid session id",
        )
    )


def get_driver_current_url(driver, fallback=""):
    try:
        return driver.current_url
    except WebDriverException:
        return fallback


def get_driver_page_source(driver, fallback=""):
    try:
        return driver.page_source
    except WebDriverException:
        return fallback


def get_driver_title(driver, fallback=""):
    try:
        return driver.title or fallback
    except WebDriverException:
        return fallback


def recover_instagram_driver(driver, url, headless_mode, profile_path):
    log_instagram("Janela do Chrome fechou durante a coleta. Reabrindo navegador do worker.")

    try:
        driver.quit()
    except Exception:
        pass

    recovered_driver = create_driver(headless=headless_mode, profile_path=profile_path)
    if url:
        recovered_driver.get(url)

    return recovered_driver, headless_mode


def resolve_writable_output_path(path):
    if not path.exists():
        return path

    try:
        with path.open("ab"):
            return path
    except PermissionError:
        return path.with_name(f"{path.stem}_updated{path.suffix}")


def get_execution_output_dir(output_dir=None):
    execution_root_dir = Path(output_dir) if output_dir else OUTPUT_ROOT_DIR / datetime.now().strftime("%d-%m-%y--%H-%M")

    if execution_root_dir.name.lower() == OUTPUT_SUBDIR_NAME:
        return execution_root_dir

    return execution_root_dir / OUTPUT_SUBDIR_NAME


def save_results(records, output_dir=None):
    execution_output_dir = get_execution_output_dir(output_dir)
    execution_output_dir.mkdir(parents=True, exist_ok=True)
    json_output_path = resolve_writable_output_path(execution_output_dir / "insta.json")
    xlsx_output_path = resolve_writable_output_path(execution_output_dir / "insta.xlsx")
    categorized_records = build_categorized_output(records)

    json_output_path.write_text(
        json.dumps(categorized_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_instagram_workbook(xlsx_output_path, categorized_records)

    return json_output_path, xlsx_output_path


def resolve_instagram_review_paths(review_path):
    review_path = Path(review_path).expanduser()

    if not review_path.is_absolute():
        review_path = Path.cwd() / review_path

    review_path = review_path.resolve()

    if review_path.is_file():
        if review_path.name.lower() != "insta.json":
            raise FileNotFoundError(
                f"Arquivo de review invalido: {review_path}. Esperado insta.json."
            )
        return review_path, review_path.parent

    if not review_path.exists():
        raise FileNotFoundError(f"Caminho de review nao encontrado: {review_path}")

    direct_json_path = review_path / "insta.json"
    nested_json_path = review_path / OUTPUT_SUBDIR_NAME / "insta.json"

    if direct_json_path.is_file():
        return direct_json_path, review_path

    if nested_json_path.is_file():
        return nested_json_path, review_path

    raise FileNotFoundError(
        "Nao foi encontrado insta.json no caminho informado. "
        f"Verifique {review_path} ou {nested_json_path.parent}."
    )


def load_instagram_records_for_review(review_path):
    json_path, output_dir = resolve_instagram_review_paths(review_path)
    categorized_records = json.loads(json_path.read_text(encoding="utf-8"))
    loaded_records = []

    for category, records in categorized_records.items():
        if not isinstance(records, list):
            continue

        for record in records:
            if not isinstance(record, dict):
                continue

            loaded_record = dict(record)
            loaded_record["categoria"] = loaded_record.get("categoria", category)
            loaded_record["fonte"] = loaded_record.get("fonte", "")
            loaded_record["status"] = loaded_record.get("status", 0)
            loaded_record["error"] = loaded_record.get("error", "")
            loaded_record["data"] = loaded_record.get("data", 0)
            if loaded_record["categoria"] == "post":
                loaded_record.update(normalize_instagram_post_data(loaded_record))
            elif loaded_record["categoria"] == "reel":
                loaded_record.update(normalize_instagram_reel_data(loaded_record))
            loaded_records.append(loaded_record)

    return loaded_records, json_path, output_dir


def build_google_query(search_term, after=None, before=None):
    query_parts = ["site:instagram.com"]

    if search_term:
        query_parts.append(f'"{search_term}"')

    if after:
        query_parts.append(f"after:{after}")

    if before:
        query_parts.append(f"before:{before}")

    return " ".join(query_parts)


def normalize_google_result_url(href):
    if not href:
        return None

    target_url = None

    if href.startswith("/url?"):
        parsed_href = urlparse(href)
        target_url = parse_qs(parsed_href.query).get("q", [None])[0]
    elif href.startswith(("http://", "https://")):
        target_url = href

    if not target_url:
        return None

    parsed_target = urlparse(target_url)
    host = parsed_target.netloc.lower()

    if "google." in host or "instagram.com" not in host:
        return None

    return parsed_target._replace(fragment="").geturl()


def categorize_instagram_url(url):
    path_parts = [part for part in urlparse(url).path.split("/") if part]

    if not path_parts:
        return "nao_identificado"

    first_path_part = path_parts[0].lower()

    if first_path_part == "p":
        return "post"

    if first_path_part in {"reel", "reels"}:
        return "reel"

    if len(path_parts) == 1 and first_path_part not in INSTAGRAM_PROFILE_RESERVED_PATHS:
        return "perfil"

    return "nao_identificado"


def build_categorized_output(records):
    categorized_records = {category: [] for category in INSTAGRAM_CATEGORY_ORDER}

    for record in records:
        category = record.get("categoria", "nao_identificado")
        categorized_records.setdefault(category, [])
        normalized_record = {
            "fonte": record.get("fonte", ""),
            "status": record.get("status", 0),
            "error": record.get("error", ""),
            "data": record.get("data", 0),
        }

        if category == "post":
            normalized_record.update(
                {
                    "perfil_publicador": record.get("perfil_publicador", ""),
                    "verificado": record.get("verificado", ""),
                    "descricao_post": record.get("descricao_post", ""),
                    "numero_likes": record.get("numero_likes", ""),
                    "numero_comentarios": record.get("numero_comentarios", ""),
                    "numero_reposts": record.get("numero_reposts", ""),
                }
            )
        elif category == "reel":
            normalized_record.update(
                {
                    "perfil_publicador": record.get("perfil_publicador", ""),
                    "verificado": record.get("verificado", ""),
                    "descricao_reel": record.get("descricao_reel", ""),
                    "numero_likes": record.get("numero_likes", ""),
                    "numero_comentarios": record.get("numero_comentarios", ""),
                    "numero_reposts": record.get("numero_reposts", ""),
                }
            )

        categorized_records[category].append(normalized_record)

    return categorized_records


def get_headers_for_category(category):
    if category == "post":
        return INSTAGRAM_POST_HEADERS
    if category == "reel":
        return INSTAGRAM_REEL_HEADERS

    return INSTAGRAM_DEFAULT_HEADERS


def get_excel_column_name(column_number):
    column_name = ""

    while column_number > 0:
        column_number, remainder = divmod(column_number - 1, 26)
        column_name = chr(65 + remainder) + column_name

    return column_name


def build_excel_cell(cell_reference, value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{cell_reference}"><v>{value}</v></c>'

    escaped_value = escape("" if value is None else str(value))
    return (
        f'<c r="{cell_reference}" t="inlineStr">'
        f"<is><t>{escaped_value}</t></is>"
        f"</c>"
    )


def build_excel_sheet_xml(records, headers):
    rows = []

    for row_index, row_values in enumerate([headers, *[
        [record.get(header, "") for header in headers]
        for record in records
    ]], start=1):
        cells = []
        for column_index, value in enumerate(row_values, start=1):
            cell_reference = f"{get_excel_column_name(column_index)}{row_index}"
            cells.append(build_excel_cell(cell_reference, value))

        rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows)}</sheetData>"
        "</worksheet>"
    )


def write_instagram_workbook(path, categorized_records):
    workbook_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        "<sheets>",
    ]
    workbook_rels_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    content_types_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]

    for sheet_index, category in enumerate(INSTAGRAM_CATEGORY_ORDER, start=1):
        workbook_xml.append(
            f'<sheet name="{escape(category)}" sheetId="{sheet_index}" r:id="rId{sheet_index}"/>'
        )
        workbook_rels_xml.append(
            '<Relationship '
            f'Id="rId{sheet_index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{sheet_index}.xml"/>'
        )
        content_types_xml.append(
            '<Override '
            f'PartName="/xl/worksheets/sheet{sheet_index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    workbook_xml.extend(["</sheets>", "</workbook>"])
    workbook_rels_xml.append("</Relationships>")
    content_types_xml.append("</Types>")

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as workbook_file:
        workbook_file.writestr(
            "[Content_Types].xml",
            "".join(content_types_xml),
        )
        workbook_file.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        workbook_file.writestr("xl/workbook.xml", "".join(workbook_xml))
        workbook_file.writestr("xl/_rels/workbook.xml.rels", "".join(workbook_rels_xml))

        for sheet_index, category in enumerate(INSTAGRAM_CATEGORY_ORDER, start=1):
            workbook_file.writestr(
                f"xl/worksheets/sheet{sheet_index}.xml",
                build_excel_sheet_xml(
                    categorized_records.get(category, []),
                    get_headers_for_category(category),
                ),
            )


def is_instagram_scrape_blocked(driver):
    current_url = get_driver_current_url(driver).lower()
    page_source = get_driver_page_source(driver).lower()
    blocked_markers = [
        "captcha",
        "challenge_required",
        "checkpoint_required",
        "please wait a few minutes before you try again",
        "sorry, this page isn't available",
        "page isn't available",
        "login • instagram",
        "entrar • instagram",
    ]
    blocked_paths = [
        "/accounts/login",
        "/challenge/",
        "/checkpoint/",
    ]

    if any(path in current_url for path in blocked_paths):
        return True

    return any(marker in page_source for marker in blocked_markers)


def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_search_text(value):
    cleaned_value = clean_text(value)
    if not cleaned_value:
        return ""

    return (
        unicodedata.normalize("NFKD", cleaned_value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )


def parse_count(value):
    if value in (None, ""):
        return ""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)

    normalized_text = clean_text(value).lower()
    normalized_text = normalized_text.replace("\xa0", " ")
    number_match = re.search(r"(\d+(?:[.,]\d+)?)", normalized_text)
    if not number_match:
        return ""

    try:
        number_text = number_match.group(1)
        multiplier = 1
        suffix_text = normalized_text[number_match.end():].strip()
        suffix_match = re.match(
            r"^(mil|mi|bi|k|m|b)\b",
            suffix_text,
        )

        if suffix_match:
            suffix = suffix_match.group(1)
            if suffix in ("mil", "k"):
                multiplier = 1_000
            elif suffix in ("mi", "m"):
                multiplier = 1_000_000
            elif suffix in ("bi", "b"):
                multiplier = 1_000_000_000
        elif re.search(r"\b(million|milhao|milhoes)\b", normalized_text):
            multiplier = 1_000_000
        elif re.search(r"\b(billion|bilhao|bilhoes)\b", normalized_text):
            multiplier = 1_000_000_000

        if multiplier > 1:
            if "," in number_text and "." in number_text:
                decimal_text = number_text.replace(".", "").replace(",", ".")
            elif "," in number_text:
                decimal_text = number_text.replace(",", ".")
            else:
                decimal_text = number_text

            decimal_number = float(decimal_text)
            return int(decimal_number * multiplier)

        return int(re.sub(r"[^\d]", "", number_text))
    except ValueError:
        return ""


def normalize_instagram_media_data(data=None, defaults=None, description_field="descricao_post"):
    normalized_data = dict(defaults or {})

    if isinstance(data, dict):
        normalized_data.update(data)

    for key in (
        "perfil_publicador",
        "verificado",
        description_field,
        "data",
        "media_url",
        "media_type",
        "drive_file_id",
        "drive_url",
    ):
        normalized_data[key] = clean_text(normalized_data.get(key, ""))

    normalized_data["perfil_publicador"] = normalized_data["perfil_publicador"].lstrip("@")

    return normalized_data


def normalize_instagram_post_data(data=None):
    return normalize_instagram_media_data(
        data=data,
        defaults=INSTAGRAM_POST_DATA_DEFAULTS,
        description_field="descricao_post",
    )


def normalize_instagram_reel_data(data=None):
    return normalize_instagram_media_data(
        data=data,
        defaults=INSTAGRAM_REEL_DATA_DEFAULTS,
        description_field="descricao_reel",
    )


def is_instagram_login_popup_present(driver):
    page_source = get_driver_page_source(driver).lower()

    if not page_source:
        return False

    for close_xpath in INSTAGRAM_LOGIN_POPUP_CLOSE_XPATHS:
        try:
            close_buttons = driver.find_elements(By.XPATH, close_xpath)
            if any(button.is_displayed() for button in close_buttons):
                return True
        except Exception:
            continue

    if not any(marker in page_source for marker in INSTAGRAM_LOGIN_POPUP_TEXT_MARKERS):
        return False

    try:
        dialog_elements = driver.find_elements(By.XPATH, "//div[@role='dialog']")
        if any(dialog.is_displayed() for dialog in dialog_elements):
            return True
    except Exception:
        pass

    return False


def has_instagram_media_content(driver):
    page_source = get_driver_page_source(driver)
    normalized_source = page_source.lower()

    if (
        "application/ld+json" in normalized_source
        or '"is_verified":' in normalized_source
        or '"edge_media_to_comment"' in normalized_source
        or '"edge_media_preview_like"' in normalized_source
    ):
        return True

    content_selectors = [
        "main time[datetime]",
        "main video",
        "main a[href*='/reel/']",
        "main a[href*='/p/']",
        "main svg[aria-label='Curtir']",
        "main svg[aria-label='Like']",
        "main svg[aria-label='Comentar']",
        "main svg[aria-label='Comment']",
    ]

    for selector in content_selectors:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            continue

    return False


def get_visible_instagram_captcha_marker(driver):
    captcha_selectors = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[title*='captcha' i]",
        ".g-recaptcha",
        ".h-captcha",
        "[data-sitekey]",
        "textarea[name='g-recaptcha-response']",
        "textarea[name='h-captcha-response']",
    ]

    for selector in captcha_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if any(element.is_displayed() for element in elements):
                return selector
        except Exception:
            continue

    return None


def get_instagram_captcha_marker(driver, page_source, page_title):
    visible_marker = get_visible_instagram_captcha_marker(driver)
    if visible_marker:
        return f"visible:{visible_marker}"

    source_markers = [
        "g-recaptcha",
        "h-captcha",
        "recaptcha/api.js",
        "hcaptcha.com/1/api.js",
        'name="g-recaptcha-response"',
        'name="h-captcha-response"',
        "security code",
        "enter the code",
        "confirm you're not a robot",
        "confirm you are not a robot",
    ]

    for marker in source_markers:
        if marker in page_source:
            return f"source:{marker}"

    title_markers = [
        "confirm you're not a robot",
        "confirm you are not a robot",
    ]

    for marker in title_markers:
        if marker in page_title:
            return f"title:{marker}"

    return None


def get_instagram_scrape_block(driver):
    current_url = get_driver_current_url(driver).lower()
    current_host = urlparse(current_url).netloc.lower()
    page_source = get_driver_page_source(driver).lower()
    page_title = get_driver_title(driver).lower()

    if not current_url and not page_source:
        return {
            "type": "webdriver_disconnected",
            "error": "instagram_webdriver_disconnected",
            "detail": "Conexao com o navegador foi perdida (invalid session/devtools).",
            "manual_resolution": False,
        }
    captcha_marker = get_instagram_captcha_marker(driver, page_source, page_title)

    if is_instagram_login_popup_present(driver):
        return None

    if "/challenge/" in current_url or "challenge_required" in page_source:
        return {
            "type": "challenge_required",
            "error": "instagram_challenge_required",
            "detail": "Instagram solicitou challenge/verificacao.",
            "manual_resolution": True,
        }

    if "/checkpoint/" in current_url or "checkpoint_required" in page_source:
        return {
            "type": "checkpoint_required",
            "error": "instagram_checkpoint_required",
            "detail": "Instagram solicitou checkpoint/verificacao de conta.",
            "manual_resolution": True,
        }

    if "instagram." in current_host and captcha_marker:
        return {
            "type": "captcha",
            "error": "instagram_captcha",
            "detail": f"Instagram exibiu captcha. marcador={captcha_marker}",
            "manual_resolution": True,
        }

    if "/accounts/login" in current_url:
        return {
            "type": "login_required",
            "error": "instagram_login_required",
            "detail": "Instagram redirecionou para login.",
            "manual_resolution": True,
        }

    if "please wait a few minutes before you try again" in page_source:
        return {
            "type": "rate_limit",
            "error": "instagram_rate_limit",
            "detail": "Instagram pediu para aguardar alguns minutos antes de tentar novamente.",
            "manual_resolution": False,
        }

    if "sorry, this page isn't available" in page_source or "page isn't available" in page_source:
        return {
            "type": "page_unavailable",
            "error": "instagram_page_unavailable",
            "detail": "Pagina do Instagram indisponivel ou removida.",
            "manual_resolution": False,
        }

    return None


def is_instagram_scrape_blocked(driver):
    return get_instagram_scrape_block(driver) is not None


def click_instagram_login_popup_close_with_script(driver):
    return driver.execute_script(
        """
        const dialogs = Array.from(document.querySelectorAll('div[role="dialog"]'));
        for (const dialog of dialogs) {
            if (!dialog.offsetParent && getComputedStyle(dialog).position !== 'fixed') {
                continue;
            }

            const labelledNodes = Array.from(dialog.querySelectorAll('[aria-label], title'));
            for (const node of labelledNodes) {
                const label = (
                    node.getAttribute('aria-label') || node.textContent || ''
                ).trim().toLowerCase();
                if (!['fechar', 'close'].includes(label)) {
                    continue;
                }

                const clickable = node.closest('button, [role="button"], a');
                if (!clickable) {
                    continue;
                }

                clickable.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                clickable.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                clickable.click();
                return true;
            }
        }
        return false;
        """
    )


def close_instagram_login_popup(driver, timeout=3):
    deadline = time.time() + timeout

    try:
        if click_instagram_login_popup_close_with_script(driver):
            time.sleep(0.5)
            if not is_instagram_login_popup_present(driver):
                log_instagram("Popup de login do Instagram fechado.")
                return True
    except Exception:
        pass

    for close_xpath in INSTAGRAM_LOGIN_POPUP_CLOSE_XPATHS:
        while time.time() < deadline:
            close_buttons = driver.find_elements(By.XPATH, close_xpath)
            visible_buttons = [button for button in close_buttons if button.is_displayed()]

            if not visible_buttons:
                break

            close_button = visible_buttons[0]

            try:
                driver.execute_script("arguments[0].click();", close_button)
            except Exception:
                try:
                    close_button.click()
                except Exception:
                    break

            time.sleep(0.5)

            if not any(
                button.is_displayed()
                for button in driver.find_elements(By.XPATH, close_xpath)
            ):
                log_instagram("Popup de login do Instagram fechado.")
                return True

    try:
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true}));"
        )
        time.sleep(0.5)
        if not is_instagram_login_popup_present(driver):
            log_instagram("Popup de login do Instagram fechado com Escape.")
            return True
    except Exception:
        pass

    return False


def format_publication_date(value):
    if value in (None, "", 0, "0"):
        return ""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed_datetime = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return parsed_datetime.strftime("%Y-%m-%d %H:%M:%S")

    cleaned_value = clean_text(value)
    if not cleaned_value:
        return ""

    if cleaned_value.isdigit():
        parsed_datetime = datetime.fromtimestamp(int(cleaned_value), tz=timezone.utc)
        return parsed_datetime.strftime("%Y-%m-%d %H:%M:%S")

    iso_candidate = cleaned_value.replace("Z", "+00:00")
    try:
        parsed_datetime = datetime.fromisoformat(iso_candidate)
        return parsed_datetime.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return cleaned_value


def load_json_objects_from_scripts(soup):
    json_objects = []

    for script in soup.select('script[type="application/ld+json"]'):
        script_content = script.string or script.get_text(strip=True)
        if not script_content:
            continue

        try:
            loaded_json = json.loads(script_content)
        except json.JSONDecodeError:
            continue

        if isinstance(loaded_json, list):
            json_objects.extend(item for item in loaded_json if isinstance(item, dict))
        elif isinstance(loaded_json, dict):
            json_objects.append(loaded_json)

    return json_objects


def extract_count_from_interaction_statistic(interaction_statistic):
    if isinstance(interaction_statistic, dict):
        interaction_statistic = [interaction_statistic]

    if not isinstance(interaction_statistic, list):
        return ""

    for statistic in interaction_statistic:
        if not isinstance(statistic, dict):
            continue

        interaction_type = statistic.get("interactionType")
        if isinstance(interaction_type, dict):
            interaction_type = interaction_type.get("@type", "")

        if interaction_type and "like" in str(interaction_type).lower():
            return parse_count(statistic.get("userInteractionCount"))

    return ""


def extract_post_data_from_json_objects(json_objects):
    extracted_data = normalize_instagram_post_data()

    for json_object in json_objects:
        author = json_object.get("author")
        if isinstance(author, dict):
            if not extracted_data.get("perfil_publicador"):
                extracted_data["perfil_publicador"] = clean_text(
                    author.get("alternateName")
                    or author.get("identifier")
                    or author.get("name")
                ).lstrip("@")

        if not extracted_data.get("descricao_post"):
            extracted_data["descricao_post"] = clean_text(
                json_object.get("caption")
                or json_object.get("description")
            )

        if extracted_data.get("numero_likes", "") == "":
            extracted_data["numero_likes"] = extract_count_from_interaction_statistic(
                json_object.get("interactionStatistic")
            )

        if extracted_data.get("numero_comentarios", "") == "":
            extracted_data["numero_comentarios"] = parse_count(json_object.get("commentCount"))

        if extracted_data.get("numero_reposts", "") == "":
            extracted_data["numero_reposts"] = parse_count(
                json_object.get("shareCount")
                or json_object.get("reshareCount")
                or json_object.get("repostCount")
            )

        if not extracted_data.get("data"):
            extracted_data["data"] = format_publication_date(
                json_object.get("uploadDate")
                or json_object.get("datePublished")
                or json_object.get("dateCreated")
            )

    return normalize_instagram_post_data(extracted_data)


def decode_embedded_json_string(value):
    if value in (None, ""):
        return ""

    text = str(value).strip()
    if not text:
        return ""

    try:
        return json.loads(f'"{text}"')
    except Exception:
        return text.replace("\\/", "/").replace("\\u0026", "&")


def normalize_media_url(value):
    text = decode_embedded_json_string(value)
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        return ""
    return text


def infer_instagram_media_type(media_url):
    normalized = (media_url or "").lower()
    if any(marker in normalized for marker in (".mp4", "/video/", "video_url")):
        return "video"
    if normalized:
        return "image"
    return ""


def first_media_url_from_value(value):
    if isinstance(value, str):
        return normalize_media_url(value)
    if isinstance(value, dict):
        for key in ("contentUrl", "thumbnailUrl", "url", "embedUrl"):
            media_url = first_media_url_from_value(value.get(key))
            if media_url:
                return media_url
        return ""
    if isinstance(value, list):
        for item in value:
            media_url = first_media_url_from_value(item)
            if media_url:
                return media_url
    return ""


def extract_media_fields_from_json_objects(json_objects):
    for json_object in json_objects:
        if not isinstance(json_object, dict):
            continue

        video_url = (
            first_media_url_from_value(json_object.get("video"))
            or first_media_url_from_value(json_object.get("contentUrl"))
            or first_media_url_from_value(json_object.get("embedUrl"))
        )
        if video_url:
            return {"media_url": video_url, "media_type": "video"}

        image_url = (
            first_media_url_from_value(json_object.get("image"))
            or first_media_url_from_value(json_object.get("thumbnailUrl"))
        )
        if image_url:
            return {"media_url": image_url, "media_type": "image"}

    return {"media_url": "", "media_type": ""}


def extract_media_fields_from_page_source(page_source, soup):
    for property_name, media_type in (
        ("og:video", "video"),
        ("og:video:url", "video"),
        ("og:image", "image"),
    ):
        meta = soup.find("meta", attrs={"property": property_name})
        media_url = normalize_media_url(meta.get("content")) if meta else ""
        if media_url:
            return {"media_url": media_url, "media_type": media_type}

    for pattern, media_type in (
        (r'"video_url":"([^"]+)"', "video"),
        (r'"display_url":"([^"]+)"', "image"),
        (r'"thumbnail_src":"([^"]+)"', "image"),
    ):
        match = re.search(pattern, page_source)
        if not match:
            continue
        media_url = normalize_media_url(match.group(1))
        if media_url:
            return {"media_url": media_url, "media_type": media_type}

    return {"media_url": "", "media_type": ""}


def extract_comments_from_page_source(page_source, max_comments=50):
    comments = []
    seen = set()
    patterns = (
        r'"text":"((?:\\.|[^"\\]){1,1000})".{0,800}?"username":"([^"]+)"',
        r'"username":"([^"]+)".{0,800}?"text":"((?:\\.|[^"\\]){1,1000})"',
    )

    for pattern_index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, page_source, flags=re.S):
            if pattern_index == 0:
                raw_text, author = match.group(1), match.group(2)
            else:
                author, raw_text = match.group(1), match.group(2)

            text = clean_text(decode_embedded_json_string(raw_text))
            author = clean_text(decode_embedded_json_string(author)).lstrip("@")
            if not text or len(text) < 2:
                continue

            key = (author, text)
            if key in seen:
                continue
            seen.add(key)
            comments.append(
                {
                    "comment_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{author}:{text}")),
                    "author": author,
                    "text": text,
                    "likes": 0,
                    "created_at": None,
                }
            )
            if len(comments) >= max_comments:
                return comments

    return comments


def extract_post_data_from_page_source(page_source):
    soup = BeautifulSoup(page_source, "html.parser")
    json_objects = load_json_objects_from_scripts(soup)
    extracted_data = normalize_instagram_post_data(extract_post_data_from_json_objects(json_objects))
    extracted_data.update(extract_media_fields_from_json_objects(json_objects))

    meta_title = soup.find("meta", attrs={"property": "og:title"})
    meta_description = soup.find("meta", attrs={"property": "og:description"})
    time_element = soup.find("time")

    if not extracted_data.get("data") and time_element:
        extracted_data["data"] = format_publication_date(time_element.get("datetime"))

    if not extracted_data.get("descricao_post") and meta_title:
        title_content = clean_text(meta_title.get("content", ""))
        title_match = re.search(r'Instagram:\s*"(.+?)"', title_content)
        if title_match:
            extracted_data["descricao_post"] = clean_text(title_match.group(1))

    if not extracted_data.get("perfil_publicador") and meta_title:
        title_content = clean_text(meta_title.get("content", ""))
        title_match = re.match(r"([^:]+?) on Instagram", title_content, flags=re.IGNORECASE)
        if title_match:
            extracted_data["perfil_publicador"] = clean_text(title_match.group(1)).lstrip("@")

    if meta_description:
        description_content = clean_text(meta_description.get("content", ""))

        for field, rule in INSTAGRAM_METRIC_EXTRACTION_RULES.items():
            if extracted_data.get(field, "") != "":
                continue

            extracted_value = extract_metric_from_text_candidates(
                [description_content],
                rule["keywords"],
                extra_patterns=rule["extra_patterns"],
            )
            if extracted_value != "":
                extracted_data[field] = extracted_value

        if not extracted_data.get("descricao_post"):
            description_match = re.search(r':\s*"(.+?)"', description_content)
            if description_match:
                extracted_data["descricao_post"] = clean_text(description_match.group(1))

    owner_match = re.search(r'"owner":\{.*?"username":"([^"]+)"', page_source, flags=re.S)
    if owner_match and not extracted_data.get("perfil_publicador"):
        extracted_data["perfil_publicador"] = clean_text(owner_match.group(1)).lstrip("@")

    verified_match = re.search(r'"is_verified":(true|false)', page_source)
    if verified_match:
        extracted_data["verificado"] = "sim" if verified_match.group(1) == "true" else "nao"

    if extracted_data.get("numero_likes", "") == "":
        likes_match = re.search(
            r'"edge_media_preview_like":\{"count":(\d+)\}|'
            r'"edge_liked_by":\{"count":(\d+)\}',
            page_source,
        )
        if likes_match:
            extracted_data["numero_likes"] = parse_count(likes_match.group(1) or likes_match.group(2))

    if extracted_data.get("numero_comentarios", "") == "":
        comments_match = re.search(
            r'"edge_media_to_parent_comment":\{"count":(\d+)\}|'
            r'"edge_media_to_comment":\{"count":(\d+)\}',
            page_source,
        )
        if comments_match:
            extracted_data["numero_comentarios"] = parse_count(
                comments_match.group(1) or comments_match.group(2)
            )

    if extracted_data.get("numero_reposts", "") == "":
        reposts_match = re.search(
            r'"share_count":(\d+)|'
            r'"reshare_count":(\d+)|'
            r'"repost_count":(\d+)|'
            r'"edge_media_to_reshare":\{"count":(\d+)\}|'
            r'"edge_media_to_repost":\{"count":(\d+)\}',
            page_source,
        )
        if reposts_match:
            extracted_data["numero_reposts"] = parse_count(
                reposts_match.group(1)
                or reposts_match.group(2)
                or reposts_match.group(3)
                or reposts_match.group(4)
                or reposts_match.group(5)
            )

    if not extracted_data.get("data"):
        publication_date_match = re.search(
            r'"taken_at_timestamp":(\d+)|'
            r'"taken_at":(\d+)|'
            r'"datePublished":"([^"]+)"|'
            r'"uploadDate":"([^"]+)"',
            page_source,
        )
        if publication_date_match:
            extracted_data["data"] = format_publication_date(
                publication_date_match.group(1)
                or publication_date_match.group(2)
                or publication_date_match.group(3)
                or publication_date_match.group(4)
            )

    if not extracted_data.get("media_url"):
        extracted_data.update(extract_media_fields_from_page_source(page_source, soup))

    if os.getenv("INSTAGRAM_FETCH_COMMENTS", "false").strip().lower() in {"1", "true", "yes", "on"}:
        extracted_data["_comments"] = extract_comments_from_page_source(
            page_source,
            max_comments=int(os.getenv("INSTAGRAM_COMMENTS_PER_POST", "50")),
        )

    extracted_data["data"] = format_publication_date(extracted_data["data"])

    return normalize_instagram_post_data(extracted_data)


def extract_instagram_profile_from_href(href):
    if not href:
        return ""

    path_parts = [part for part in urlparse(href).path.split("/") if part]
    if len(path_parts) != 1:
        return ""

    username = clean_text(path_parts[0]).lstrip("@")
    if not username or username.lower() in INSTAGRAM_PROFILE_RESERVED_PATHS:
        return ""

    return username


def is_likely_instagram_caption_text(text):
    normalized_text = normalize_search_text(text)
    if len(text) < 20 or not normalized_text:
        return False

    rejected_markers = [
        "entrar",
        "cadastre se",
        "curtir",
        "responder",
        "carregar mais comentarios",
        "para curtir ou comentar",
        "mais posts de",
        "ver mais posts",
        "audio original",
    ]
    if any(marker in normalized_text for marker in rejected_markers):
        return False

    if parse_compact_metric_text(text) != "":
        return False

    return True


def extract_caption_from_rendered_soup(soup):
    time_element = soup.find("time")
    if time_element:
        search_elements = time_element.find_all_next(["span", "div"], limit=160)
    else:
        search_elements = soup.select("span, div")

    for element in search_elements:
        if element.name == "div" and element.find(["span", "div"]):
            continue

        text = clean_text(element.get_text(" ", strip=True))
        if is_likely_instagram_caption_text(text):
            return text

    return ""


def capture_post_page_snapshot(driver):
    rendered_html_fragments = []
    seen_html = set()
    for selector in ("article", "main"):
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements:
            try:
                html = element.get_attribute("outerHTML")
            except Exception:
                continue

            html = html or ""
            if not clean_text(html) or html in seen_html:
                continue

            rendered_html_fragments.append(html)
            seen_html.add(html)

    return {
        "page_source": get_driver_page_source(driver),
        "rendered_html_fragments": rendered_html_fragments,
    }


def build_post_soups_from_snapshot(page_source, rendered_html_fragments=None):
    soups = []
    seen_html = set()

    for html in rendered_html_fragments or []:
        html = html or ""
        if not clean_text(html) or html in seen_html:
            continue

        soups.append(BeautifulSoup(html, "html.parser"))
        seen_html.add(html)

    if not soups:
        soups.append(BeautifulSoup(page_source, "html.parser"))

    return soups


def collect_metric_text_candidates_from_soup(soup):
    candidates = []
    seen_texts = set()

    for element in soup.select("[aria-label], [title], button, a, span, div, li, time"):
        raw_values = [
            element.get("aria-label", ""),
            element.get("title", ""),
            element.get_text(" ", strip=True),
        ]

        for raw_value in raw_values:
            text = clean_text(raw_value)
            normalized_text = normalize_search_text(text)
            if (
                not text
                or not normalized_text
                or len(text) > 160
                or normalized_text in seen_texts
            ):
                continue

            candidates.append(text)
            seen_texts.add(normalized_text)

    return candidates


def parse_compact_metric_text(text):
    cleaned_text = clean_text(text)
    if not cleaned_text:
        return ""

    normalized_text = normalize_search_text(cleaned_text)
    if not re.fullmatch(r"\d+(?:[.,]\d+)?(?:\s*(?:mil|mi|bi|k|m|b))?", normalized_text):
        return ""

    return parse_count(cleaned_text)


def classify_instagram_metric_action(label_text):
    normalized_label = normalize_search_text(label_text)
    if not normalized_label:
        return ""

    if any(keyword in normalized_label for keyword in ("curtir", "like")):
        return "numero_likes"
    if any(keyword in normalized_label for keyword in ("comentar", "comment")):
        return "numero_comentarios"
    if any(keyword in normalized_label for keyword in ("compartilhar", "repostar", "repost", "share")):
        return "numero_reposts"

    return ""


def iter_instagram_metric_action_nodes(soup):
    action_nodes = []
    seen_nodes = set()

    for svg in soup.select("svg[aria-label], svg[title]"):
        label_text = svg.get("aria-label") or svg.get("title") or ""
        metric_field = classify_instagram_metric_action(label_text)
        if metric_field and id(svg) not in seen_nodes:
            action_nodes.append({"field": metric_field, "node": svg})
            seen_nodes.add(id(svg))

    for title_element in soup.select("svg title"):
        metric_field = classify_instagram_metric_action(title_element.get_text(" ", strip=True))
        svg = title_element.parent
        if metric_field and svg is not None and id(svg) not in seen_nodes:
            action_nodes.append({"field": metric_field, "node": svg})
            seen_nodes.add(id(svg))

    return action_nodes


def select_instagram_metric_action_bar(action_nodes):
    ancestor_candidates = {}

    for action_node in action_nodes:
        current = action_node["node"]
        for depth in range(6):
            current = getattr(current, "parent", None)
            if current is None or not getattr(current, "name", None):
                break

            entry = ancestor_candidates.setdefault(
                id(current),
                {
                    "node": current,
                    "fields": set(),
                    "depth_score": 0,
                    "text_length": 0,
                },
            )
            entry["fields"].add(action_node["field"])
            entry["depth_score"] += depth + 1

    ranked_candidates = []
    for entry in ancestor_candidates.values():
        if len(entry["fields"]) < 3:
            continue

        entry["text_length"] = len(clean_text(entry["node"].get_text(" ", strip=True)))
        ranked_candidates.append(entry)

    if not ranked_candidates:
        return None

    ranked_candidates.sort(
        key=lambda entry: (
            -len(entry["fields"]),
            entry["text_length"],
            entry["depth_score"],
        )
    )
    return ranked_candidates[0]["node"]


def extract_metric_count_from_adjacent_nodes(node, container):
    current = node

    while current is not None and current is not container:
        for sibling in current.next_siblings:
            if not getattr(sibling, "name", None):
                continue

            direct_value = parse_compact_metric_text(sibling.get_text(" ", strip=True))
            if direct_value != "":
                return direct_value

            for nested_element in sibling.find_all(["span", "div", "a"], limit=6):
                nested_value = parse_compact_metric_text(nested_element.get_text(" ", strip=True))
                if nested_value != "":
                    return nested_value

        current = getattr(current, "parent", None)

    return ""


def extract_metrics_from_instagram_action_bar(soup):
    extracted_metrics = {}
    action_nodes = iter_instagram_metric_action_nodes(soup)
    action_bar = select_instagram_metric_action_bar(action_nodes)
    if action_bar is None:
        return extracted_metrics

    for action_node in action_nodes:
        if action_node["field"] in extracted_metrics:
            continue
        if action_bar not in getattr(action_node["node"], "parents", []):
            continue

        extracted_value = extract_metric_count_from_adjacent_nodes(
            action_node["node"],
            action_bar,
        )
        if extracted_value != "":
            extracted_metrics[action_node["field"]] = extracted_value

    return extracted_metrics


def parse_count_parts(number_part, suffix_part=""):
    value_parts = [clean_text(number_part), clean_text(suffix_part)]
    combined_value = " ".join(part for part in value_parts if part)
    return parse_count(combined_value)


def extract_metric_values_from_text(text, keywords, extra_patterns=None):
    normalized_text = normalize_search_text(text)
    if not normalized_text:
        return []

    normalized_keywords = [normalize_search_text(keyword) for keyword in keywords]
    normalized_keywords = [keyword for keyword in normalized_keywords if keyword]
    if not normalized_keywords:
        return []

    count_pattern = r"(?P<count>\d+(?:[.,]\d+)?)\s*(?P<suffix>mil|mi|bi|k|m|b)?"
    keywords_pattern = "|".join(re.escape(keyword) for keyword in normalized_keywords)
    regex_patterns = [
        rf"{count_pattern}\s+(?:{keywords_pattern})\b",
        rf"(?:{keywords_pattern})\b[\s:,-]*{count_pattern}",
    ]

    if extra_patterns:
        regex_patterns.extend(extra_patterns)

    extracted_values = []
    for pattern in regex_patterns:
        for match in re.finditer(pattern, normalized_text):
            parsed_value = parse_count_parts(match.group("count"), match.groupdict().get("suffix", ""))
            if parsed_value != "":
                extracted_values.append(parsed_value)

    return extracted_values


def extract_metric_from_text_candidates(text_candidates, keywords, extra_patterns=None):
    extracted_values = []

    for text in text_candidates:
        extracted_values.extend(
            extract_metric_values_from_text(
                text,
                keywords,
                extra_patterns=extra_patterns,
            )
        )

    if not extracted_values:
        return ""

    return max(extracted_values)


def extract_post_data_from_rendered_soups(soups):
    extracted_data = normalize_instagram_post_data()

    all_metric_candidates = []

    for soup in soups:
        if not extracted_data.get("data"):
            time_element = soup.find("time")
            if time_element:
                extracted_data["data"] = format_publication_date(
                    time_element.get("datetime") or time_element.get_text(" ", strip=True)
                )

        if not extracted_data.get("perfil_publicador"):
            for anchor in soup.select("header a[href], a[href]"):
                profile_name = extract_instagram_profile_from_href(anchor.get("href", ""))
                if profile_name:
                    extracted_data["perfil_publicador"] = profile_name
                    break

        if not extracted_data.get("descricao_post"):
            heading = soup.find("h1")
            if heading:
                description_text = clean_text(heading.get_text(" ", strip=True))
                if description_text:
                    extracted_data["descricao_post"] = description_text

        if not extracted_data.get("descricao_post"):
            caption_text = extract_caption_from_rendered_soup(soup)
            if caption_text:
                extracted_data["descricao_post"] = caption_text

        if not extracted_data.get("verificado"):
            for element in soup.select("[aria-label], [title], title"):
                marker_text = clean_text(
                    element.get("aria-label")
                    or element.get("title")
                    or element.get_text(" ", strip=True)
                )
                normalized_marker = normalize_search_text(marker_text)
                if any(
                    keyword in normalized_marker
                    for keyword in ("verified", "verificado", "meta verified")
                ):
                    extracted_data["verificado"] = "sim"
                    break

        for field, value in extract_metrics_from_instagram_action_bar(soup).items():
            if extracted_data.get(field, "") == "":
                extracted_data[field] = value

        all_metric_candidates.extend(collect_metric_text_candidates_from_soup(soup))

    for field, rule in INSTAGRAM_METRIC_EXTRACTION_RULES.items():
        if extracted_data.get(field, "") != "":
            continue

        extracted_value = extract_metric_from_text_candidates(
            all_metric_candidates,
            rule["keywords"],
            extra_patterns=rule["extra_patterns"],
        )
        if extracted_value != "":
            extracted_data[field] = extracted_value

    return normalize_instagram_post_data(extracted_data)


def extract_post_data_from_snapshot(snapshot):
    page_source = snapshot.get("page_source", "")
    rendered_html_fragments = snapshot.get("rendered_html_fragments", [])
    rendered_data = extract_post_data_from_rendered_soups(
        build_post_soups_from_snapshot(page_source, rendered_html_fragments)
    )
    extracted_data = normalize_instagram_post_data(extract_post_data_from_page_source(page_source))

    for field in INSTAGRAM_POST_DATA_DEFAULTS:
        if rendered_data.get(field) not in (None, ""):
            extracted_data[field] = rendered_data[field]

    return normalize_instagram_post_data(extracted_data)


def extract_post_data_from_driver(driver):
    return extract_post_data_from_snapshot(capture_post_page_snapshot(driver))


def prepare_media_record_for_enrichment(record):
    record.setdefault("perfil_publicador", "")
    record.setdefault("verificado", "")
    record.setdefault("descricao_post", "")
    record.setdefault("descricao_reel", "")
    record.setdefault("numero_likes", "")
    record.setdefault("numero_comentarios", "")
    record.setdefault("numero_reposts", "")
    record.setdefault("media_url", "")
    record.setdefault("media_type", "")
    record.setdefault("drive_file_id", "")
    record.setdefault("drive_url", "")
    record.setdefault("_comments", [])
    record.setdefault("error", "")


def adapt_extracted_media_data_for_record(record, extracted_data):
    category = record.get("categoria")

    if category == "reel":
        return normalize_instagram_reel_data(
            {
                "perfil_publicador": extracted_data.get("perfil_publicador", ""),
                "verificado": extracted_data.get("verificado", ""),
                "descricao_reel": extracted_data.get("descricao_post", ""),
                "numero_likes": extracted_data.get("numero_likes", ""),
                "numero_comentarios": extracted_data.get("numero_comentarios", ""),
                "numero_reposts": extracted_data.get("numero_reposts", ""),
                "media_url": extracted_data.get("media_url", ""),
                "media_type": extracted_data.get("media_type", ""),
                "drive_file_id": extracted_data.get("drive_file_id", ""),
                "drive_url": extracted_data.get("drive_url", ""),
                "_comments": extracted_data.get("_comments", []),
                "data": extracted_data.get("data", ""),
            }
        )

    return normalize_instagram_post_data(extracted_data)


def finalize_post_parse_job(parse_job, total_posts, records, output_dir=None):
    if not parse_job:
        return

    index = parse_job["index"]
    record = parse_job["record"]
    future = parse_job["future"]

    try:
        extracted_data = future.result()
        record.update(extracted_data)
        record["status"] = 2
        record["error"] = ""
        log_instagram(
            f"[posts {format_progress(index, total_posts)}] "
            "Post enriquecido com sucesso. "
            f"likes={record.get('numero_likes', '')}, "
            f"comentarios={record.get('numero_comentarios', '')}, "
            f"reposts={record.get('numero_reposts', '')}"
        )
    except Exception as exc:
        record["status"] = 1
        record["error"] = clean_text(exc)
        log_instagram(
            f"[posts {format_progress(index, total_posts)}] "
            f"Falha ao enriquecer post: {record['error']}"
        )

    save_results(records, output_dir=output_dir)
    log_instagram(
        f"[posts {format_progress(index, total_posts)}] "
        f"Checkpoint salvo. resumo={summarize_record_counts(records)}"
    )


def wait_for_post_page_content(driver, timeout=15):
    deadline = time.time() + timeout

    while time.time() < deadline:
        close_instagram_login_popup(driver, timeout=1)

        if has_instagram_media_content(driver):
            return

        if is_instagram_scrape_blocked(driver):
            return

        if is_instagram_login_popup_present(driver):
            time.sleep(0.5)
            continue

        if (
            driver.find_elements(By.TAG_NAME, "article")
            and driver.find_elements(By.CSS_SELECTOR, "article svg[aria-label]")
        ):
            return

        time.sleep(0.5)


def restore_headless_mode_for_post(driver, headless_mode, profile_path, timeout=15):
    if headless_mode:
        return driver, headless_mode

    log_instagram("Captcha resolvido na pagina do post. Retornando ao modo headless.")
    current_url = get_driver_current_url(driver)
    driver = reopen_driver(driver, current_url, headless=True, profile_path=profile_path)
    headless_mode = True
    wait_for_post_page_content(driver, timeout=timeout)
    return driver, headless_mode


def wait_for_post_scrape_page(driver, headless_mode, profile_path, timeout=15, target_url=None):
    started_headless = headless_mode
    wait_for_post_page_content(driver, timeout=timeout)
    driver, headless_mode = wait_for_instagram_block_resolution(driver, headless_mode, profile_path)

    if target_url and started_headless and not headless_mode and not get_instagram_scrape_block(driver):
        log_instagram(f"Reabrindo post original apos resolucao manual: {target_url}")
        driver.get(target_url)
        wait_for_post_page_content(driver, timeout=timeout)
        driver, headless_mode = wait_for_instagram_block_resolution(driver, headless_mode, profile_path)

    driver, headless_mode = restore_headless_mode_for_post(
        driver,
        headless_mode,
        profile_path,
        timeout=timeout,
    )
    return driver, headless_mode


def enrich_single_media_record(record, index, total_posts, driver, headless_mode, profile_path):
    prepare_media_record_for_enrichment(record)

    extracted_data = {}
    status = 1
    error = ""
    media_label = "reel" if record.get("categoria") == "reel" else "post"

    for attempt in range(2):
        try:
            log_instagram(
                f"[midia {format_progress(index, total_posts)}] "
                f"Abrindo {media_label}: {record['fonte']}"
            )
            driver.get(record["fonte"])
            driver, headless_mode = wait_for_post_scrape_page(
                driver,
                headless_mode,
                profile_path,
                target_url=record["fonte"],
            )

            if is_instagram_login_popup_present(driver):
                close_instagram_login_popup(driver, timeout=2)
                wait_for_post_page_content(driver, timeout=5)

            block = get_instagram_scrape_block(driver)
            if block:
                error = block["error"]
                log_instagram(
                    f"[midia {format_progress(index, total_posts)}] "
                    f"Bloqueio detectado ao abrir o {media_label}. "
                    f"tipo={block['type']}, erro={block['error']}, "
                    f"manual={block['manual_resolution']}, detalhe={block['detail']}, "
                    f"url={get_driver_current_url(driver, record['fonte'])}"
                )
            elif is_instagram_login_popup_present(driver) and not has_instagram_media_content(driver):
                error = "popup_login_instagram"
                log_instagram(
                    f"[midia {format_progress(index, total_posts)}] "
                    f"Popup de login persistiu e impediu a coleta do {media_label}."
                )
            else:
                snapshot = capture_post_page_snapshot(driver)
                extracted_data = adapt_extracted_media_data_for_record(
                    record,
                    extract_post_data_from_snapshot(snapshot),
                )
                status = 2
                error = ""
                log_instagram(
                    f"[midia {format_progress(index, total_posts)}] "
                    f"{media_label.capitalize()} enriquecido com sucesso. "
                    f"likes={extracted_data.get('numero_likes', '')}, "
                    f"comentarios={extracted_data.get('numero_comentarios', '')}, "
                    f"reposts={extracted_data.get('numero_reposts', '')}"
                )

            break
        except Exception as exc:
            if attempt == 0 and is_webdriver_window_lost_error(exc):
                driver, headless_mode = recover_instagram_driver(
                    driver,
                    record["fonte"],
                    headless_mode,
                    profile_path,
                )
                continue

            error = clean_text(exc)
            log_instagram(
                f"[midia {format_progress(index, total_posts)}] "
                f"Falha ao enriquecer {media_label}: {error}"
            )
            break

    return {
        "status": status,
        "error": error,
        "data": extracted_data,
        "driver": driver,
        "headless_mode": headless_mode,
    }


def enrich_post_records(
    records,
    driver,
    headless_mode,
    profile_path,
    output_dir=None,
    persist_to_disk=True,
):
    post_records = [
        record
        for record in records
        if record.get("categoria") in {"post", "reel"}
    ]
    pending_post_records = [record for record in post_records if record.get("status") != 2]

    log_instagram(
        "Iniciando enriquecimento de posts/reels. "
        f"midias_total={len(post_records)}, pendentes={len(pending_post_records)}"
    )

    if not pending_post_records:
        return records, driver, headless_mode

    worker_count = min(get_instagram_post_worker_count(), len(pending_post_records))
    checkpoint_interval = get_instagram_post_checkpoint_interval()
    indexed_post_records = {
        id(record): index for index, record in enumerate(post_records, start=1)
    }
    pending_records_queue = Queue()
    state_lock = threading.Lock()
    progress_state = {"completed": 0, "last_saved": 0}

    for record in pending_post_records:
        pending_records_queue.put(record)

    worker_contexts = create_instagram_worker_contexts(
        driver,
        headless_mode,
        profile_path,
        worker_count,
    )
    log_instagram(
        "Paralelismo de extracao configurado. "
        f"workers={worker_count}, checkpoint_interval={checkpoint_interval}"
    )

    def flush_checkpoint_if_needed(force=False):
        should_save = force or (
            progress_state["completed"] - progress_state["last_saved"] >= checkpoint_interval
        )
        if not should_save:
            return

        if persist_to_disk:
            save_results(records, output_dir=output_dir)
        progress_state["last_saved"] = progress_state["completed"]
        log_instagram(
            "Checkpoint registrado durante o enriquecimento paralelo. "
            f"concluidos={progress_state['completed']}, resumo={summarize_record_counts(records)}"
        )

    def worker_loop(worker_context):
        local_driver = worker_context["driver"]
        local_headless_mode = worker_context["headless_mode"]
        local_profile_path = worker_context["profile_path"]
        worker_id = worker_context["worker_id"]

        while True:
            try:
                record = pending_records_queue.get_nowait()
            except Empty:
                break

            index = indexed_post_records[id(record)]
            result = enrich_single_media_record(
                record,
                index,
                len(post_records),
                local_driver,
                local_headless_mode,
                local_profile_path,
            )
            local_driver = result["driver"]
            local_headless_mode = result["headless_mode"]

            with state_lock:
                record.update(result["data"])
                record["status"] = result["status"]
                record["error"] = result["error"]
                progress_state["completed"] += 1
                flush_checkpoint_if_needed(force=result["status"] != 2)

            log_instagram(
                f"[worker {worker_id}] "
                f"Finalizado post {format_progress(index, len(post_records))}. "
                f"status={record['status']}, erro={record.get('error', '') or '-'}"
            )

        worker_context["driver"] = local_driver
        worker_context["headless_mode"] = local_headless_mode

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(worker_loop, context) for context in worker_contexts]
            for future in futures:
                future.result()

        with state_lock:
            flush_checkpoint_if_needed(force=True)

        primary_context = worker_contexts[0]
        returned_driver = primary_context["driver"]
        returned_headless_mode = primary_context["headless_mode"]
        cleanup_instagram_worker_contexts(worker_contexts, preserve_driver=returned_driver)
        return records, returned_driver, returned_headless_mode
    except Exception:
        cleanup_instagram_worker_contexts(
            worker_contexts,
            preserve_driver=worker_contexts[0]["driver"],
        )
        raise


def extract_instagram_results(page_source):
    soup = BeautifulSoup(page_source, "html.parser")
    records = []
    seen_links = set()

    for anchor in soup.select("a[href]"):
        link = normalize_google_result_url(anchor.get("href", ""))

        if not link or link in seen_links:
            continue

        seen_links.add(link)
        records.append(
            {
                "fonte": link,
                "categoria": categorize_instagram_url(link),
                "status": 0,
                "error": "",
                "data": 0,
            }
        )

    return records


def is_instagram_logged_in(driver):
    return driver.get_cookie("sessionid") is not None or driver.get_cookie("ds_user_id") is not None


def wait_for_login_completion(driver, platform_name, login_check, poll_interval=5):
    log_instagram(f"Aguardando login no {platform_name}...")

    while not login_check(driver):
        time.sleep(poll_interval)

    log_instagram(f"Login no {platform_name} concluido.")


def reopen_driver(driver, url, headless, profile_path):
    try:
        driver.quit()
    except Exception:
        pass

    new_driver = create_driver(headless=headless, profile_path=profile_path)
    new_driver.get(url)
    return new_driver


def export_instagram_session_cookies(driver):
    try:
        current_host = urlparse(driver.current_url).netloc.lower()
    except Exception:
        current_host = ""

    if "instagram.com" not in current_host:
        driver.get("https://www.instagram.com/")

    allowed_keys = {"name", "value", "domain", "path", "expiry", "secure", "httpOnly", "sameSite"}
    exported_cookies = []

    for cookie in driver.get_cookies():
        cookie_domain = clean_text(cookie.get("domain", "")).lower()
        if "instagram.com" not in cookie_domain:
            continue

        exported_cookie = {
            key: value
            for key, value in cookie.items()
            if key in allowed_keys and value is not None
        }
        if exported_cookie.get("expiry") is not None:
            try:
                exported_cookie["expiry"] = int(exported_cookie["expiry"])
            except (TypeError, ValueError):
                exported_cookie.pop("expiry", None)

        exported_cookies.append(exported_cookie)

    return exported_cookies


def bootstrap_instagram_session(driver, instagram_cookies):
    driver.get("https://www.instagram.com/")

    for cookie in instagram_cookies:
        try:
            driver.add_cookie(cookie)
        except Exception:
            continue

    driver.get("https://www.instagram.com/")
    close_instagram_login_popup(driver, timeout=1)
    return driver


def create_instagram_worker_contexts(driver, headless_mode, profile_path, worker_count):
    worker_contexts = [
        {
            "worker_id": 1,
            "driver": driver,
            "headless_mode": headless_mode,
            "profile_path": profile_path,
            "temporary_profile_path": None,
        }
    ]

    if worker_count <= 1:
        return worker_contexts

    instagram_cookies = export_instagram_session_cookies(driver)
    worker_parent = (
        _docker_chrome_session_root()
        if _chrome_in_docker() and not _docker_allow_chrome_profile_on_app_volume()
        else RUNTIME_PROFILES_DIR
    )
    worker_parent.mkdir(parents=True, exist_ok=True)

    for worker_id in range(2, worker_count + 1):
        temporary_profile_path = Path(
            tempfile.mkdtemp(
                prefix=f"insta_worker_{worker_id}_",
                dir=str(worker_parent),
            )
        )
        worker_driver = create_driver(headless=headless_mode, profile_path=temporary_profile_path)
        bootstrap_instagram_session(worker_driver, instagram_cookies)
        worker_contexts.append(
            {
                "worker_id": worker_id,
                "driver": worker_driver,
                "headless_mode": headless_mode,
                "profile_path": temporary_profile_path,
                "temporary_profile_path": temporary_profile_path,
            }
        )

    return worker_contexts


def cleanup_instagram_worker_contexts(worker_contexts, preserve_driver=None):
    preserved_driver = preserve_driver or (worker_contexts[0]["driver"] if worker_contexts else None)

    for context in worker_contexts:
        driver = context.get("driver")
        temporary_profile_path = context.get("temporary_profile_path")

        if driver is not None and driver is not preserved_driver:
            try:
                driver.quit()
            except Exception:
                pass

        if temporary_profile_path:
            try:
                for child in sorted(temporary_profile_path.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                temporary_profile_path.rmdir()
            except Exception:
                pass


def wait_for_instagram_block_resolution(driver, headless_mode, profile_path):
    message_shown = False
    block = get_instagram_scrape_block(driver)
    twocaptcha_captcha_rounds = 0

    while block and block["manual_resolution"]:
        if _chrome_in_docker():
            log_instagram(
                "Docker: bloqueio Instagram que exigiria acao manual — "
                "nao aguardando; segue com o que ja foi possivel coletar."
            )
            break
        if headless_mode:
            if (
                block.get("type") == "captcha"
                and twocaptcha_captcha_rounds < 2
                and try_twocaptcha_solve_current_page(driver)
            ):
                twocaptcha_captcha_rounds += 1
                time.sleep(4)
                close_instagram_login_popup(driver, timeout=1)
                block = get_instagram_scrape_block(driver)
                if not block or not block.get("manual_resolution"):
                    break
                continue

            log_instagram(
                f"ALERTA: Bloqueio no Instagram. {block['detail']} "
                "Resolva no navegador para continuar."
            )
            log_instagram(
                "Bloqueio manual detectado em headless. "
                f"tipo={block['type']}, erro={block['error']}. Abrindo navegador visivel."
            )
            driver = reopen_driver(
                driver,
                get_driver_current_url(driver, "https://www.instagram.com/"),
                headless=False,
                profile_path=profile_path,
            )
            headless_mode = False
            message_shown = False
            block = get_instagram_scrape_block(driver)
            continue

        if not message_shown:
            log_instagram(
                "Aguardando resolucao manual no Instagram. "
                f"tipo={block['type']}, erro={block['error']}, detalhe={block['detail']}, "
                f"url={get_driver_current_url(driver)}"
            )
            message_shown = True

        time.sleep(5)
        close_instagram_login_popup(driver, timeout=1)
        block = get_instagram_scrape_block(driver)

    return driver, headless_mode


def restore_headless_mode(driver, headless_mode, profile_path, timeout=15):
    if headless_mode:
        return driver, headless_mode

    log_instagram("Captcha resolvido. Retornando ao modo headless.")
    driver = reopen_driver(
        driver,
        get_driver_current_url(driver, "https://www.google.com/"),
        headless=True,
        profile_path=profile_path,
    )
    headless_mode = True

    try:
        WebDriverWait(driver, timeout).until(
            lambda current_driver: extract_instagram_results(current_driver.page_source)
            or current_driver.find_elements(By.CSS_SELECTOR, "#search")
            or current_driver.find_elements(By.CSS_SELECTOR, "a#pnnext")
        )
    except TimeoutException:
        pass

    return driver, headless_mode


def _driver_on_google_sorry_page(driver) -> bool:
    try:
        return "/sorry/" in (driver.current_url or "").lower()
    except Exception:
        return False


def _google_serp_ready(driver) -> bool:
    """True apenas em SERP real; /sorry/index?continue=...search contem 'search' na query e enganava o wait."""
    if _driver_on_google_sorry_page(driver):
        return False
    try:
        if extract_instagram_results(driver.page_source):
            return True
    except Exception:
        pass
    try:
        if driver.find_elements(By.CSS_SELECTOR, "a#pnnext"):
            return True
    except Exception:
        pass
    try:
        p = urlparse(driver.current_url or "")
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        if "google." in host and path.startswith("/search"):
            return bool(driver.find_elements(By.CSS_SELECTOR, "#search"))
    except Exception:
        pass
    return False


def wait_for_results_page(driver, headless_mode, profile_path, timeout=30):
    """Aguarda elementos típicos da página de resultados do Google (sem bloquear em captcha)."""
    profile_path = Path(profile_path)
    log_instagram("Aguardando página de resultados...")

    try:
        WebDriverWait(driver, timeout).until(_google_serp_ready)
        _save_google_netscape_cookies(driver)
        log_instagram("Página de resultados carregada!")
    except TimeoutException:
        on_sorry = _driver_on_google_sorry_page(driver)
        if on_sorry:
            log_instagram(
                "Google respondeu com /sorry/ (verificacao ou trafego incomum). "
                "Sem SERP valida ate resolver o desafio."
            )
        else:
            log_instagram("Timeout - usando página atual")

        if on_sorry and not get_twocaptcha_api_key():
            log_instagram(
                "Google /sorry/: TWOCAPTCHA_API_KEY vazia no processo — 2Captcha nao foi chamado. "
                "Defina a chave no .env usado pelo `docker compose --env-file` (ex.: .env.dev) "
                "e repasse no docker-compose (servico feeder), ou coloque-a no ficheiro .env "
                "montado em /app/.env; depois `docker compose build feeder && docker compose up -d`."
            )

        if get_twocaptcha_api_key() and (
            on_sorry or _google_page_shows_captcha_widget(driver)
        ):
            if try_twocaptcha_solve_current_page(driver):
                time.sleep(4)
                try:
                    WebDriverWait(driver, min(timeout, 60)).until(_google_serp_ready)
                    _save_google_netscape_cookies(driver)
                    log_instagram("SERP carregada apos 2Captcha.")
                except TimeoutException:
                    log_instagram(
                        "Ainda sem SERP apos 2Captcha. Verifique saldo, chave TWOCAPTCHA_API_KEY "
                        "ou defina INSTAGRAM_USE_GOOGLE_COOKIES_FILE=1 com cookies Google exportados."
                    )
            elif on_sorry:
                log_instagram(
                    "2Captcha nao resolveu (widget ausente ou erro). "
                    "Opcoes: ajustar TWOCAPTCHA_API_KEY ou usar INSTAGRAM_USE_GOOGLE_COOKIES_FILE=1."
                )

    return driver, True, profile_path


def get_next_page_url(driver):
    next_selectors = [
        "a#pnnext",
        "a[aria-label='Next page']",
    ]

    for selector in next_selectors:
        next_links = driver.find_elements(By.CSS_SELECTOR, selector)
        for next_link in next_links:
            href = next_link.get_attribute("href")
            if href:
                return href

    current_url = urlparse(get_driver_current_url(driver))
    current_query = parse_qs(current_url.query)
    current_start = int(current_query.get("start", ["0"])[0])

    for next_link in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        href = next_link.get_attribute("href")
        if not href or "google." not in urlparse(href).netloc.lower():
            continue

        parsed_href = urlparse(href)
        if "/search" not in parsed_href.path:
            continue

        next_query = parse_qs(parsed_href.query)
        next_start = next_query.get("start", [None])[0]
        if next_start is None:
            continue

        try:
            if int(next_start) > current_start:
                return href
        except ValueError:
            continue

    return None


def _maybe_save_results(records, output_dir, persist_to_disk):
    """Wrapper para gravar JSON/XLSX apenas quando persist_to_disk=True."""
    if not persist_to_disk:
        return None, None
    return save_results(records, output_dir=output_dir)


def InstaMain(
    search_term="",
    after=None,
    before=None,
    output_dir=None,
    target=None,
    persist_to_disk=True,
):
    """Coleta links do Instagram via Google e enriquece posts/reels.

    Parâmetros:
      search_term       Termo de busca a ser pesquisado.
      after, before     Filtros de data do Google (YYYY-MM-DD).
      output_dir        Diretório de saída para JSON/XLSX (só se persist_to_disk).
      target            Soft-cap: para a paginação ao acumular >= target registros.
                        None = coleta tudo o que o Google entregar.
      persist_to_disk   Se True, grava insta.json/insta.xlsx; se False, retorna
                        apenas a lista (modo usado pela integração GuaIA / banco).
    """
    google_query = build_google_query(search_term, after=after, before=before)
    url = f"https://www.google.com/search?q={quote_plus(google_query)}"
    collected_records = []
    seen_links = set()
    visited_pages = set()
    page_number = 0
    headless_mode = True
    runtime_profile_path = get_runtime_profile_path()
    driver = create_driver(headless=headless_mode, profile_path=runtime_profile_path)

    try:
        log_instagram(
            "Iniciando busca no Instagram via Google. "
            f"termo={search_term or '<vazio>'}, after={after or '-'}, before={before or '-'}, "
            f"target={target if target is not None else 'sem limite'}"
        )
        driver.get(url)

        while True:
            try:
                driver, headless_mode, runtime_profile_path = wait_for_results_page(
                    driver,
                    headless_mode,
                    runtime_profile_path,
                )
            except WebDriverException as exc:
                if is_webdriver_window_lost_error(exc):
                    fallback_url = get_driver_current_url(driver, url)
                    driver, headless_mode = recover_instagram_driver(
                        driver,
                        fallback_url,
                        headless_mode,
                        runtime_profile_path,
                    )
                    continue
                raise

            current_page_url = get_driver_current_url(driver)
            if current_page_url in visited_pages:
                log_instagram("Pagina ja visitada detectada. Encerrando paginacao.")
                break

            visited_pages.add(current_page_url)
            page_number += 1
            log_instagram(
                f"[paginas {page_number}] Processando resultados: {current_page_url}"
            )

            page_records = extract_instagram_results(get_driver_page_source(driver))
            new_records_count = 0

            for record in page_records:
                if record["fonte"] in seen_links:
                    continue

                seen_links.add(record["fonte"])
                collected_records.append(record)
                new_records_count += 1

            _maybe_save_results(collected_records, output_dir, persist_to_disk)
            log_instagram(
                f"[paginas {page_number}] "
                f"links_encontrados={len(page_records)}, novos={new_records_count}, "
                f"total_acumulado={len(collected_records)}, resumo={summarize_record_counts(collected_records)}"
            )

            if target is not None and len(collected_records) >= target:
                log_instagram(
                    f"Target de {target} registros atingido. Encerrando paginacao."
                )
                break

            next_page_url = get_next_page_url(driver)
            if not next_page_url or next_page_url in visited_pages:
                log_instagram("Nao ha proxima pagina nova. Encerrando coleta de resultados.")
                break

            log_instagram(f"[paginas {page_number}] Indo para a proxima pagina do Google.")
            driver.get(next_page_url)
            time.sleep(3)  # Delay anti-detecção

        # Aplica o limite final antes de enriquecer (evita abrir N posts a mais)
        if target is not None and len(collected_records) > target:
            collected_records = collected_records[:target]

        collected_records, driver, headless_mode = enrich_post_records(
            collected_records,
            driver,
            headless_mode,
            runtime_profile_path,
            output_dir=output_dir,
            persist_to_disk=persist_to_disk,
        )
    finally:
        saved_json_path, saved_xlsx_path = _maybe_save_results(
            collected_records, output_dir, persist_to_disk
        )
        try:
            driver.quit()
        except Exception:
            pass

    log_instagram(
        "Execucao concluida. "
        f"paginas={page_number}, total_registros={len(collected_records)}, "
        f"resumo={summarize_record_counts(collected_records)}"
    )
    if persist_to_disk:
        print(json.dumps(build_categorized_output(collected_records), ensure_ascii=False, indent=2))
        log_instagram(f"Arquivos salvos em: {saved_json_path} e {saved_xlsx_path}")
    return collected_records


def InstaReview(review_path):
    collected_records, loaded_json_path, output_dir = load_instagram_records_for_review(review_path)
    runtime_profile_path = None
    driver = None
    headless_mode = True
    pending_records = [record for record in collected_records if record.get("status") != 2]
    pending_post_records = [
        record
        for record in collected_records
        if record.get("categoria") in {"post", "reel"} and record.get("status") != 2
    ]

    log_instagram(
        "Iniciando review do Instagram. "
        f"arquivo={loaded_json_path}, total_registros={len(collected_records)}, "
        f"pendentes={len(pending_records)}, midias_pendentes={len(pending_post_records)}"
    )

    try:
        if pending_post_records:
            runtime_profile_path = get_runtime_profile_path()
            driver = create_driver(headless=headless_mode, profile_path=runtime_profile_path)
            collected_records, driver, headless_mode = enrich_post_records(
                collected_records,
                driver,
                headless_mode,
                runtime_profile_path,
                output_dir=output_dir,
            )
        else:
            log_instagram("Nenhum post/reel pendente com status != 2 para reprocessar no review.")
    finally:
        saved_json_path, saved_xlsx_path = save_results(collected_records, output_dir=output_dir)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    log_instagram(
        "Review concluida. "
        f"total_registros={len(collected_records)}, resumo={summarize_record_counts(collected_records)}"
    )
    print(json.dumps(build_categorized_output(collected_records), ensure_ascii=False, indent=2))
    log_instagram(f"Arquivos salvos em: {saved_json_path} e {saved_xlsx_path}")
    return collected_records


def InstaLogin():
    driver = create_driver(headless=False, profile_path=get_chrome_profile_path())

    try:
        driver.get("https://www.instagram.com/accounts/login/")
        wait_for_login_completion(driver, "Instagram", is_instagram_logged_in)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Schema único de saída (compatível com o banco e com o DTO do GuaIA)
# ---------------------------------------------------------------------------

def _extract_post_id(url: str) -> str:
    """Extrai o shortcode do Instagram a partir da URL."""
    if not url:
        return ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return ""
    if parts[0].lower() in ("p", "reel", "reels", "tv") and len(parts) >= 2:
        return parts[1]
    if len(parts) == 1:
        return parts[0]
    return parts[-1]


def _to_int_or_zero(value) -> int:
    if value in (None, "", 0):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_record_for_output(record: dict) -> dict:
    """Converte um record interno (schema do runner) para o schema persistido.

    Esse schema é consumido tanto por db.py (upsert_posts) quanto pelo feeder
    GuaIA (create_instagram_dto).
    """
    url = record.get("fonte", "")
    categoria = record.get("categoria") or "nao_identificado"
    description = (
        record.get("descricao_post")
        or record.get("descricao_reel")
        or ""
    )
    verified_raw = (record.get("verificado") or "").strip().lower()
    verified = None
    if verified_raw == "sim":
        verified = True
    elif verified_raw == "nao":
        verified = False

    return {
        "url": url,
        "post_id": _extract_post_id(url),
        "categoria": categoria,
        "author": record.get("perfil_publicador") or "",
        "verified": verified,
        "description": description,
        "likes": _to_int_or_zero(record.get("numero_likes")),
        "comments": _to_int_or_zero(record.get("numero_comentarios")),
        "reposts": _to_int_or_zero(record.get("numero_reposts")),
        "media_url": record.get("media_url") or "",
        "media_type": record.get("media_type") or infer_instagram_media_type(record.get("media_url")),
        "drive_file_id": record.get("drive_file_id") or "",
        "drive_url": record.get("drive_url") or "",
        "_comments": record.get("_comments") or [],
        "status": int(record.get("status") or 0),
        "error": record.get("error") or "",
        "published_at": record.get("data") or "",
    }


# ---------------------------------------------------------------------------
# InstaScraper – interface async no padrão do KwaiScraper
# ---------------------------------------------------------------------------

class InstaScraper:
    """Wrapper async em torno de InstaMain.

    O Instagram não expõe API pública estável, então toda a coleta é feita via
    Selenium + Chrome. Como o Selenium é síncrono, usamos asyncio.to_thread
    para integrar a execução bloqueante ao loop async (mesmo padrão usado pelo
    download de vídeos no KwaiScraper).

    Mantém a interface mínima esperada pelos feeders/dump:
      - search_posts(keyword, target=...)  -> list[str] (URLs únicas)
      - posts: dict[str, dict]              (URL -> registro normalizado)
      - fetch_comments_all(...)             (no-op por enquanto)
    """

    def __init__(
        self,
        headless: bool = True,
        search_term: str = "",
        after: str | None = None,
        before: str | None = None,
    ):
        # headless é mantido por compatibilidade com a CLI; o runner já decide
        # automaticamente abrir browser visível quando há captcha/bloqueio.
        self.headless = headless
        self._search_term = search_term or ""
        self._after = after
        self._before = before
        self.posts: dict[str, dict] = {}

    async def search_posts(
        self,
        keyword: str,
        target: int = 200,
        fresh: bool = False,
    ) -> list[str]:
        """Executa a coleta no Google -> Instagram.

        ``fresh`` é aceito por compatibilidade com a interface do KwaiScraper,
        mas o runner do Instagram não faz cache de progresso anterior, então o
        parâmetro é ignorado.
        """
        del fresh

        records = await asyncio.to_thread(
            InstaMain,
            keyword,
            self._after,
            self._before,
            None,
            target,
            False,
        )

        for record in records or []:
            normalized = _normalize_record_for_output(record)
            url = normalized["url"]
            if not url or url in self.posts:
                continue
            self.posts[url] = normalized

        log_instagram(
            f"Coleta async concluida. unicos={len(self.posts)}, alvo={target}"
        )
        return list(self.posts.keys())[:target]

    async def fetch_comments_all(
        self,
        posts: list[dict],
        max_per_post: int = 50,
        concurrency: int = 3,
    ) -> None:
        """Não implementado.

        O Instagram não tem API pública estável de comentários e o popup de
        login bloqueia o scraping anônimo. Mantemos a assinatura para
        compatibilidade com o KwaiScraper, deixando ``_comments=[]``.
        """
        del max_per_post, concurrency
        for post in posts:
            post.setdefault("_comments", [])


# ---------------------------------------------------------------------------
# CLI – padrão idêntico ao kwai_collector.collector
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, "")
    if v.lower() in ("1", "true", "yes"):
        return True
    if v.lower() in ("0", "false", "no"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, ""))
    except (TypeError, ValueError):
        return default


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Instagram Post Collector (Selenium + GuaIA)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-s", "--search", default=None, metavar="TERMO",
        help="Termo de busca (sobrepoe SEARCH_TERM do .env).",
    )
    parser.add_argument(
        "-n", "--target", type=int, default=None, metavar="N",
        help="Quantidade alvo de posts.",
    )
    parser.add_argument(
        "--after", default=None, metavar="YYYY-MM-DD",
        help="Filtra resultados posteriores a essa data (Google after:).",
    )
    parser.add_argument(
        "--before", default=None, metavar="YYYY-MM-DD",
        help="Filtra resultados anteriores a essa data (Google before:).",
    )
    parser.add_argument(
        "--login", action="store_true",
        help="Abre o navegador para login no Instagram e encerra.",
    )
    return parser.parse_args()


async def _async_main():
    args = _parse_args()

    if args.login:
        await asyncio.to_thread(InstaLogin)
        return

    SEARCH_TERM = args.search or os.getenv("SEARCH_TERM")
    if not SEARCH_TERM:
        raise SystemExit(
            "ERRO: SEARCH_TERM nao definido. "
            "Defina a variavel de ambiente SEARCH_TERM ou use --search <termo>."
        )

    TARGET_POSTS = args.target or _env_int("TARGET_POSTS", 200)

    db = None
    run_id = None
    pg_host = os.getenv("POSTGRES_HOST", "")
    if pg_host:
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
            run_id = await db.create_run(SEARCH_TERM, TARGET_POSTS)
            print(f"  PostgreSQL conectado | run_id={run_id}\n")
        except Exception as exc:
            print(f"  Aviso: DB indisponivel ({exc}). Continuando sem banco.\n")
            db = None

    scraper = InstaScraper(
        search_term=SEARCH_TERM,
        after=args.after,
        before=args.before,
    )
    urls = await scraper.search_posts(SEARCH_TERM, target=TARGET_POSTS)

    if not urls:
        print("Nenhum post coletado.")
        if db:
            await db.finish_run(run_id, 0)
            await db.close()
        return

    results = [scraper.posts.get(u, {"url": u}) for u in urls]

    saved_to_db = 0
    if db and results:
        try:
            saved_to_db = await db.upsert_posts(results, SEARCH_TERM, run_id)
            await db.finish_run(run_id, saved_to_db)
            print(f"  {saved_to_db} posts salvos no PostgreSQL.")
        except Exception as exc:
            print(f"  Erro ao salvar no banco: {exc}")
        finally:
            await db.close()

    counts = {}
    for r in results:
        counts[r.get("categoria", "nao_identificado")] = (
            counts.get(r.get("categoria", "nao_identificado"), 0) + 1
        )

    print(f"\n{'=' * 55}")
    print(f"  Termo de busca   : {SEARCH_TERM}")
    print(f"  Posts coletados  : {len(results)}")
    for cat, n in counts.items():
        print(f"  {cat:<16} : {n}")
    if db:
        print(f"  Salvos no banco  : {saved_to_db}")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    asyncio.run(_async_main())

