#!/usr/bin/env python3
"""Download KSeF invoices for the current month.

Authentication flow (KSeF API v2):
1. POST /auth/challenge
2. GET  /security/public-key-certificates
3. POST /auth/ksef-token (encrypted "token|timestampMs")
4. GET  /auth/{referenceNumber} until status=200
5. POST /auth/token/redeem
6. Use accessToken for invoice endpoints
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_BASE_URL = "https://api.ksef.mf.gov.pl/v2"
AUTH_CONTEXT_TYPE = "Nip"
DEFAULT_DATE_TYPE = "PermanentStorage"
DEFAULT_PAGE_SIZE = 100
DEFAULT_AUTH_POLL_INTERVAL = 1.0
DEFAULT_AUTH_TIMEOUT_SECONDS = 120
DEFAULT_INVOICE_TYPE = "purchase"
DEFAULT_FILENAME_MODE = "seller-id"
DEFAULT_RENDER_MODE = "yes"
DEFAULT_TIMEZONE = "Europe/Warsaw"
DEFAULT_LOCAL_OUT_DIR = "downloads"
DEFAULT_TOKEN_FILE = "token.txt"
COUNTERPARTY_NAME_MAX_LEN = 15
TRACKING_FILE_NAME = "downloaded.txt"
MASTER_PREFIX_FILE = "dir_prefix.txt"
ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 60
RATE_LIMIT_RETRY_BUFFER_SECONDS = 0.2
DEFAULT_RATE_LIMIT_MAX_RETRIES = 8
FALLBACK_RATE_LIMITS = {
    "invoiceMetadata": {"perSecond": 8, "perMinute": 16, "perHour": 20},
    "invoiceDownload": {"perSecond": 8, "perMinute": 16, "perHour": 64},
}
INVOICE_TYPE_CONFIG = {
    "purchase": {
        "subject_type": "Subject2",
        "target_subdir": "ksef_purchase",
        "label": "purchase invoices",
    },
    "sales": {
        "subject_type": "Subject1",
        "target_subdir": "ksef_sales",
        "label": "sales invoices",
    },
}


class KsefApiError(RuntimeError):
    """Raised when KSeF API returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        headers: Optional[Any] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers
        self.body = body


@dataclass(frozen=True)
class RateLimitValues:
    per_second: int
    per_minute: int
    per_hour: int


class SlidingWindowRateLimiter:
    def __init__(self, group_name: str, limits: RateLimitValues) -> None:
        self.group_name = group_name
        self.windows = [
            (1.0, limits.per_second),
            (60.0, limits.per_minute),
            (3600.0, limits.per_hour),
        ]
        self.timestamps: List[float] = []

    def wait_for_slot(self) -> None:
        while True:
            now = time.monotonic()
            max_window_seconds = max(window_seconds for window_seconds, _ in self.windows)
            self.timestamps = [
                stamp for stamp in self.timestamps if now - stamp < max_window_seconds
            ]

            wait_seconds = 0.0
            for window_seconds, limit in self.windows:
                if limit <= 0:
                    continue
                window_start = now - window_seconds
                recent = [stamp for stamp in self.timestamps if stamp > window_start]
                if len(recent) >= limit:
                    wait_seconds = max(
                        wait_seconds,
                        window_seconds - (now - min(recent)) + RATE_LIMIT_RETRY_BUFFER_SECONDS,
                    )

            if wait_seconds <= 0:
                return

            console_info(
                f"Rate limit {self.group_name}: waiting {wait_seconds:.1f}s before next request"
            )
            time.sleep(wait_seconds)

    def record_request(self) -> None:
        self.timestamps.append(time.monotonic())


def parse_retry_after_seconds(headers: Optional[Any], body: Optional[str]) -> Optional[float]:
    retry_after_raw = None
    if headers is not None:
        try:
            retry_after_raw = headers.get("Retry-After")
        except AttributeError:
            retry_after_raw = None

    if retry_after_raw:
        text = str(retry_after_raw).strip()
        try:
            return max(0.0, float(text.replace(",", ".")))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(text)
                return max(0.0, (retry_at - datetime.now(retry_at.tzinfo)).total_seconds())
            except Exception:
                pass

    if body:
        match = re.search(
            r"po\s+(\d+(?:[\.,]\d+)?)\s+sek",
            body,
            flags=re.IGNORECASE,
        )
        if match:
            return max(0.0, float(match.group(1).replace(",", ".")))

    return None


@dataclass
class AuthResult:
    access_token: str
    refresh_token: str
    reference_number: str
    access_token_valid_until: datetime
    refresh_token_valid_until: datetime


@dataclass
class KsefClient:
    base_url: str
    bearer_token: Optional[str] = None
    timeout_seconds: int = 60
    rate_limiters: Optional[Dict[str, SlidingWindowRateLimiter]] = None
    max_rate_limit_retries: int = DEFAULT_RATE_LIMIT_MAX_RETRIES
    refresh_token: Optional[str] = None
    access_token_valid_until: Optional[datetime] = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        accept: str = "application/json",
        bearer_token: Optional[str] = None,
        rate_group: Optional[str] = None,
        allow_token_refresh: bool = True,
    ) -> bytes:
        base = self.base_url.rstrip("/")
        url = f"{base}/{path.lstrip('/')}"

        if query:
            query_string = urlencode({k: v for k, v in query.items() if v is not None})
            url = f"{url}?{query_string}"

        data: Optional[bytes] = None
        base_headers = {"Accept": accept}

        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            base_headers["Content-Type"] = "application/json"

        limiter = self.rate_limiters.get(rate_group) if self.rate_limiters and rate_group else None

        attempts = 0
        token_refreshed_after_401 = False
        while True:
            if bearer_token is None and allow_token_refresh:
                self.refresh_access_token_if_needed()

            if limiter is not None:
                limiter.wait_for_slot()
                limiter.record_request()

            effective_bearer = bearer_token if bearer_token is not None else self.bearer_token
            headers = dict(base_headers)
            if effective_bearer:
                headers["Authorization"] = f"Bearer {effective_bearer}"

            request = Request(url=url, method=method.upper(), data=data, headers=headers)

            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return response.read()
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempts < self.max_rate_limit_retries:
                    attempts += 1
                    retry_after = parse_retry_after_seconds(exc.headers, body)
                    if retry_after is None:
                        retry_after = 1.0
                    retry_after += RATE_LIMIT_RETRY_BUFFER_SECONDS
                    console_warn(
                        f"KSeF rate limit hit for {rate_group or 'request'}; "
                        f"retrying in {retry_after:.1f}s (attempt {attempts}/{self.max_rate_limit_retries})"
                    )
                    time.sleep(retry_after)
                    continue
                if (
                    exc.code == 401
                    and bearer_token is None
                    and allow_token_refresh
                    and self.refresh_token
                    and not token_refreshed_after_401
                ):
                    token_refreshed_after_401 = True
                    console_warn("Access token was rejected; refreshing token and retrying request")
                    self.refresh_access_token(force=True)
                    continue
                raise KsefApiError(
                    f"KSeF API error {exc.code} for {method.upper()} {url}: {body}",
                    status_code=exc.code,
                    headers=exc.headers,
                    body=body,
                ) from exc
            except URLError as exc:
                raise KsefApiError(f"Network error for {method.upper()} {url}: {exc}") from exc

    def post_json(
        self,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        bearer_token: Optional[str] = None,
        rate_group: Optional[str] = None,
        allow_token_refresh: bool = True,
    ) -> Dict[str, Any]:
        payload = self._request(
            "POST",
            path,
            query=query,
            json_body=json_body,
            accept="application/json",
            bearer_token=bearer_token,
            rate_group=rate_group,
            allow_token_refresh=allow_token_refresh,
        )
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise KsefApiError(
                f"Invalid JSON in response for POST {path}: {payload[:500]!r}"
            ) from exc

    def get_json(
        self,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        bearer_token: Optional[str] = None,
        rate_group: Optional[str] = None,
        allow_token_refresh: bool = True,
    ) -> Any:
        payload = self._request(
            "GET",
            path,
            query=query,
            accept="application/json",
            bearer_token=bearer_token,
            rate_group=rate_group,
            allow_token_refresh=allow_token_refresh,
        )
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise KsefApiError(
                f"Invalid JSON in response for GET {path}: {payload[:500]!r}"
            ) from exc

    def get_xml(
        self,
        path: str,
        *,
        bearer_token: Optional[str] = None,
        rate_group: Optional[str] = None,
        allow_token_refresh: bool = True,
    ) -> str:
        payload = self._request(
            "GET",
            path,
            accept="application/xml",
            bearer_token=bearer_token,
            rate_group=rate_group,
            allow_token_refresh=allow_token_refresh,
        )
        return payload.decode("utf-8", errors="replace")

    def refresh_access_token_if_needed(self) -> None:
        if not self.refresh_token or not self.access_token_valid_until:
            return

        now = datetime.now(self.access_token_valid_until.tzinfo)
        seconds_left = (self.access_token_valid_until - now).total_seconds()
        if seconds_left <= ACCESS_TOKEN_REFRESH_MARGIN_SECONDS:
            self.refresh_access_token(force=True)

    def refresh_access_token(self, *, force: bool = False) -> None:
        if not self.refresh_token:
            return

        if not force and self.access_token_valid_until:
            now = datetime.now(self.access_token_valid_until.tzinfo)
            seconds_left = (self.access_token_valid_until - now).total_seconds()
            if seconds_left > ACCESS_TOKEN_REFRESH_MARGIN_SECONDS:
                return

        response = self.post_json(
            "/auth/token/refresh",
            bearer_token=self.refresh_token,
            allow_token_refresh=False,
        )
        access_obj = response.get("accessToken") if isinstance(response, dict) else None
        access_token = access_obj.get("token") if isinstance(access_obj, dict) else None
        valid_until = access_obj.get("validUntil") if isinstance(access_obj, dict) else None

        if not isinstance(access_token, str) or not access_token:
            raise KsefApiError("Invalid /auth/token/refresh response: missing accessToken.token")
        if not isinstance(valid_until, str) or not valid_until:
            raise KsefApiError("Invalid /auth/token/refresh response: missing accessToken.validUntil")

        self.bearer_token = access_token
        self.access_token_valid_until = parse_datetime(valid_until)
        console_ok(f"Access token refreshed. Valid until: {self.access_token_valid_until.isoformat(timespec='seconds')}")


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_app_relative_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return get_app_dir() / path


def enable_ansi_colors() -> bool:
    if os.getenv("NO_COLOR"):
        return False

    if os.name != "nt":
        return sys.stdout.isatty()

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return False

        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False

        enable_virtual_terminal_processing = 0x0004
        if mode.value & enable_virtual_terminal_processing:
            return sys.stdout.isatty()

        if kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal_processing) == 0:
            return False

        return sys.stdout.isatty()
    except Exception:
        return False


COLOR_ENABLED = enable_ansi_colors()


def colorize(text: str, *codes: str) -> str:
    if not COLOR_ENABLED or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def console_section(title: str) -> None:
    print(colorize(f"\n== {title} ==", "1", "36"))


def console_info(message: str) -> None:
    print(f"{colorize('[INFO]', '36')} {message}")


def console_ok(message: str) -> None:
    print(f"{colorize('[ OK ]', '32')} {message}")


def console_warn(message: str) -> None:
    print(f"{colorize('[WARN]', '33')} {message}")


def console_error(message: str) -> None:
    print(f"{colorize('[ERR ]', '31')} {message}", file=sys.stderr)


def console_list(title: str, items: Iterable[str], *, empty_message: str) -> None:
    values = list(items)
    if not values:
        console_info(f"{title}: {empty_message}")
        return

    console_ok(f"{title} ({len(values)}):")
    for value in values:
        print(f"  - {colorize(value, '32')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download purchase or sales invoices from KSeF for the selected month and "
            "optionally render each downloaded XML into PDF.\n\n"
            "Files next to this script or EXE:\n"
            "  token.txt      KSeF token used for authentication by default.\n"
            "  dir_prefix.txt Optional master path. If present and non-empty, invoices are "
            "saved only to <dir_prefix>/<YYYY_MM>/ksef_purchase or ksef_sales.\n\n"
            "Target path behavior:\n"
            "  If dir_prefix.txt exists and contains a path, that location is used as the "
            "only download/render target.\n"
            "  If dir_prefix.txt is missing or empty, files are saved only to "
            "./downloads/<YYYY-MM>/ksef_purchase or ksef_sales next to this script or EXE.\n\n"
            "The script authenticates using the NIP embedded in the token and stores downloaded invoice IDs in "
            "downloaded.txt inside the active target folder."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help="Path to KSeF token file (default: token.txt next to the script or EXE).",
    )
    parser.add_argument(
        "--date-type",
        default=DEFAULT_DATE_TYPE,
        choices=["Issue", "Invoicing", "PermanentStorage"],
        help="Date dimension used in query filters.",
    )
    parser.add_argument(
        "--invoice-type",
        default=DEFAULT_INVOICE_TYPE,
        choices=sorted(INVOICE_TYPE_CONFIG),
        help="Invoice bucket to download: purchase -> Subject2, sales -> Subject1 (default: purchase).",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Optional year for month download window (use with --month).",
    )
    parser.add_argument(
        "--month",
        type=int,
        default=None,
        help="Optional month (1-12) for download window (use with --year).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only query metadata, do not download XML files.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files if they already exist.",
    )
    parser.add_argument(
        "--filename-mode",
        default=DEFAULT_FILENAME_MODE,
        choices=["id", "seller-id"],
        help=(
            "Output filename mode: "
            "'id' -> <ksefNumber>.xml, "
            "'seller-id' -> <counterpartyNamePrefix15>__<ksefNumber>.xml "
            "(seller for purchase invoices, buyer for sales invoices)"
        ),
    )
    parser.add_argument(
        "--render",
        default=DEFAULT_RENDER_MODE,
        choices=["yes", "no"],
        help="Render downloaded XML files to PDF using render_ksef_invoice_pdf.py (default: yes).",
    )
    return parser.parse_args()


def load_ksef_token(token_file_path: Path) -> tuple[str, str]:
    if token_file_path.exists():
        token = token_file_path.read_text(encoding="utf-8").strip()
        if not token:
            raise KsefApiError(f"Token file exists but is empty: {token_file_path}")
        return token.removeprefix("Bearer ").strip(), f"{token_file_path.resolve()}"

    token_from_env = os.getenv("KSEF_TOKEN", "").strip()
    if token_from_env:
        return token_from_env.removeprefix("Bearer ").strip(), "KSEF_TOKEN environment variable"

    raise KsefApiError(
        f"Token file not found: {token_file_path}. Set KSEF_TOKEN environment variable."
    )


def load_master_prefix() -> Optional[Path]:
    prefix_file = get_app_dir() / MASTER_PREFIX_FILE
    if not prefix_file.exists():
        return None

    raw = prefix_file.read_text(encoding="utf-8").strip()
    if not raw:
        return None

    # Support optional quotes and trailing slash/backslash in config file.
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]
    raw = raw.strip()
    if not raw:
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (get_app_dir() / path).resolve()
    return path


def rate_limit_values_from_payload(
    payload: Any,
    group_name: str,
    fallback: Dict[str, int],
) -> RateLimitValues:
    group_payload = payload.get(group_name) if isinstance(payload, dict) else None
    if not isinstance(group_payload, dict):
        console_warn(f"Rate limits: missing {group_name}; using documented fallback")
        group_payload = fallback

    try:
        return RateLimitValues(
            per_second=int(group_payload["perSecond"]),
            per_minute=int(group_payload["perMinute"]),
            per_hour=int(group_payload["perHour"]),
        )
    except (KeyError, TypeError, ValueError):
        console_warn(f"Rate limits: invalid {group_name}; using documented fallback")
        return RateLimitValues(
            per_second=int(fallback["perSecond"]),
            per_minute=int(fallback["perMinute"]),
            per_hour=int(fallback["perHour"]),
        )


def load_rate_limiters(client: KsefClient) -> Dict[str, SlidingWindowRateLimiter]:
    try:
        payload = client.get_json("/rate-limits")
        console_ok("Loaded active KSeF API rate limits")
    except KsefApiError as exc:
        console_warn(f"Could not load KSeF API rate limits: {exc}. Using documented fallbacks.")
        payload = {}

    limiters: Dict[str, SlidingWindowRateLimiter] = {}
    for group_name, fallback in FALLBACK_RATE_LIMITS.items():
        limits = rate_limit_values_from_payload(payload, group_name, fallback)
        limiters[group_name] = SlidingWindowRateLimiter(group_name, limits)
        console_info(
            f"Rate limit {group_name}: {limits.per_second}/s, "
            f"{limits.per_minute}/min, {limits.per_hour}/h"
        )
    return limiters


def load_tracking_ids(track_file: Path) -> set[str]:
    seen: set[str] = set()
    if not track_file.exists():
        return seen
    for line in track_file.read_text(encoding="utf-8").splitlines():
        invoice_id = line.strip()
        if invoice_id:
            seen.add(invoice_id)
    return seen


def write_tracking_ids(track_file: Path, invoice_ids: set[str]) -> None:
    track_file.parent.mkdir(parents=True, exist_ok=True)
    if not invoice_ids:
        track_file.write_text("", encoding="utf-8")
        return
    payload = "\n".join(sorted(invoice_ids)) + "\n"
    track_file.write_text(payload, encoding="utf-8")


def infer_nip_from_token(ksef_token: str) -> Optional[str]:
    # KSeF tokens often include a segment like "nip-1234567890".
    match = re.search(r"(?:^|\|)nip-(\d{10})(?:\||$)", ksef_token, flags=re.IGNORECASE)
    return match.group(1) if match else None


def resolve_context_nip(ksef_token: str) -> str:
    inferred_nip = infer_nip_from_token(ksef_token)
    if inferred_nip:
        return inferred_nip
    raise KsefApiError(
        "Missing NIP in token. The token must include an embedded 'nip-XXXXXXXXXX' segment."
    )


def resolve_timezone(timezone_name: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:
        if timezone_name == "Europe/Warsaw":
            local_tz = datetime.now().astimezone().tzinfo
            if local_tz is not None:
                return local_tz
        raise ValueError(f"No time zone found with key {timezone_name!r}") from exc


def month_range_now(timezone_name: str) -> tuple[datetime, datetime]:
    tz = resolve_timezone(timezone_name)
    now = datetime.now(tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, now


def month_range_for_selection(
    timezone_name: str,
    *,
    year: Optional[int],
    month: Optional[int],
) -> tuple[datetime, datetime]:
    if (year is None) != (month is None):
        raise ValueError("Use --year and --month together.")

    if year is None and month is None:
        return month_range_now(timezone_name)

    assert month is not None
    if month < 1 or month > 12:
        raise ValueError("--month must be in range 1..12")

    tz = resolve_timezone(timezone_name)
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    last_day = monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=tz)

    now = datetime.now(tz)
    if end > now:
        end = now

    return start, end


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def to_iso8601(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


POLISH_FILENAME_CHAR_MAP = str.maketrans(
    {
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ź": "z",
        "ż": "z",
        "Ą": "A",
        "Ć": "C",
        "Ę": "E",
        "Ł": "L",
        "Ń": "N",
        "Ó": "O",
        "Ś": "S",
        "Ź": "Z",
        "Ż": "Z",
    }
)


def sanitize_filename(name: str) -> str:
    # Filename-only transliteration: keep display text elsewhere unchanged.
    normalized = name.translate(POLISH_FILENAME_CHAR_MAP)

    # Keep only characters safe on Windows/Linux filesystems.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", normalized)
    return safe.strip("._") or "invoice"


def get_ksef_token_encryption_certificate(client: KsefClient) -> str:
    certs_raw = client.get_json("/security/public-key-certificates")
    if not isinstance(certs_raw, list):
        raise KsefApiError("Unexpected public-key response: expected a list")

    now = datetime.now().astimezone()
    matching: List[Dict[str, Any]] = []

    for item in certs_raw:
        if not isinstance(item, dict):
            continue
        usage = item.get("usage")
        certificate = item.get("certificate")
        valid_from = item.get("validFrom")
        valid_to = item.get("validTo")
        if not (
            isinstance(usage, list)
            and "KsefTokenEncryption" in usage
            and isinstance(certificate, str)
            and isinstance(valid_from, str)
            and isinstance(valid_to, str)
        ):
            continue

        try:
            vf = parse_datetime(valid_from)
            vt = parse_datetime(valid_to)
        except ValueError:
            continue

        if vf <= now <= vt:
            matching.append(item)

    if not matching:
        raise KsefApiError("No currently valid KSeF token-encryption public certificate found")

    matching.sort(key=lambda c: parse_datetime(c["validFrom"]), reverse=True)
    return str(matching[0]["certificate"])


def encrypt_token_with_timestamp(token: str, timestamp_ms: int, certificate_der_b64: str) -> str:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:
        raise KsefApiError(
            "Missing dependency 'cryptography'. Install it with: python -m pip install cryptography"
        ) from exc

    try:
        cert_der = base64.b64decode(certificate_der_b64)
        certificate = x509.load_der_x509_certificate(cert_der)
    except Exception as exc:
        raise KsefApiError(f"Failed to decode KSeF public certificate: {exc}") from exc

    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise KsefApiError("KSeF token-encryption certificate does not contain an RSA key")

    plaintext = f"{token}|{timestamp_ms}".encode("utf-8")

    try:
        encrypted = public_key.encrypt(
            plaintext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as exc:
        raise KsefApiError(f"Failed to encrypt KSeF token: {exc}") from exc

    return base64.b64encode(encrypted).decode("ascii")


def authenticate_by_ksef_token(
    client: KsefClient,
    *,
    ksef_token: str,
    context_value: str,
) -> AuthResult:
    if DEFAULT_AUTH_POLL_INTERVAL <= 0:
        raise ValueError("auth-poll-interval must be > 0")
    if DEFAULT_AUTH_TIMEOUT_SECONDS <= 0:
        raise ValueError("auth-timeout-seconds must be > 0")

    challenge_response = client.post_json("/auth/challenge")
    challenge = challenge_response.get("challenge")
    timestamp_ms = challenge_response.get("timestampMs")
    if not isinstance(challenge, str) or not challenge:
        raise KsefApiError("Invalid /auth/challenge response: missing challenge")
    if not isinstance(timestamp_ms, int):
        raise KsefApiError("Invalid /auth/challenge response: missing timestampMs")

    certificate_der_b64 = get_ksef_token_encryption_certificate(client)
    encrypted_token = encrypt_token_with_timestamp(
        ksef_token,
        timestamp_ms,
        certificate_der_b64,
    )

    init_response = client.post_json(
        "/auth/ksef-token",
        json_body={
            "challenge": challenge,
            "contextIdentifier": {
                "type": AUTH_CONTEXT_TYPE,
                "value": context_value,
            },
            "encryptedToken": encrypted_token,
        },
    )

    reference_number = init_response.get("referenceNumber")
    auth_token_obj = init_response.get("authenticationToken")
    auth_token = auth_token_obj.get("token") if isinstance(auth_token_obj, dict) else None

    if not isinstance(reference_number, str) or not reference_number:
        raise KsefApiError("Invalid /auth/ksef-token response: missing referenceNumber")
    if not isinstance(auth_token, str) or not auth_token:
        raise KsefApiError("Invalid /auth/ksef-token response: missing authenticationToken.token")

    deadline = time.monotonic() + DEFAULT_AUTH_TIMEOUT_SECONDS

    while True:
        status_response = client.get_json(
            f"/auth/{quote(reference_number, safe='')}",
            bearer_token=auth_token,
        )
        status = status_response.get("status", {}) if isinstance(status_response, dict) else {}
        code = status.get("code") if isinstance(status, dict) else None

        if code == 200:
            break

        if code == 100:
            if time.monotonic() >= deadline:
                raise KsefApiError(
                    f"Authentication timed out after {DEFAULT_AUTH_TIMEOUT_SECONDS}s (reference {reference_number})."
                )
            time.sleep(DEFAULT_AUTH_POLL_INTERVAL)
            continue

        description = status.get("description") if isinstance(status, dict) else None
        details = status.get("details") if isinstance(status, dict) else None
        detail_text = ""
        if isinstance(details, list) and details:
            detail_text = " | details: " + "; ".join(str(x) for x in details)
        raise KsefApiError(
            f"Authentication failed (reference {reference_number}, status={code}, description={description}){detail_text}"
        )

    tokens_response = client.post_json("/auth/token/redeem", bearer_token=auth_token)

    access_obj = tokens_response.get("accessToken") if isinstance(tokens_response, dict) else None
    refresh_obj = tokens_response.get("refreshToken") if isinstance(tokens_response, dict) else None
    access_token = access_obj.get("token") if isinstance(access_obj, dict) else None
    refresh_token = refresh_obj.get("token") if isinstance(refresh_obj, dict) else None
    access_valid_until = access_obj.get("validUntil") if isinstance(access_obj, dict) else None
    refresh_valid_until = refresh_obj.get("validUntil") if isinstance(refresh_obj, dict) else None

    if not isinstance(access_token, str) or not access_token:
        raise KsefApiError("Invalid /auth/token/redeem response: missing accessToken.token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise KsefApiError("Invalid /auth/token/redeem response: missing refreshToken.token")
    if not isinstance(access_valid_until, str) or not access_valid_until:
        raise KsefApiError("Invalid /auth/token/redeem response: missing accessToken.validUntil")
    if not isinstance(refresh_valid_until, str) or not refresh_valid_until:
        raise KsefApiError("Invalid /auth/token/redeem response: missing refreshToken.validUntil")

    return AuthResult(
        access_token=access_token,
        refresh_token=refresh_token,
        reference_number=reference_number,
        access_token_valid_until=parse_datetime(access_valid_until),
        refresh_token_valid_until=parse_datetime(refresh_valid_until),
    )


def fetch_all_metadata(
    client: KsefClient,
    *,
    subject_type: str,
    date_type: str,
    date_from: datetime,
    date_to: datetime,
) -> List[Dict[str, Any]]:
    if DEFAULT_PAGE_SIZE < 10 or DEFAULT_PAGE_SIZE > 250:
        raise ValueError("page_size must be between 10 and 250")

    page_offset = 0
    all_invoices: List[Dict[str, Any]] = []

    while True:
        filters = {
            "subjectType": subject_type,
            "dateRange": {
                "dateType": date_type,
                "from": to_iso8601(date_from),
                "to": to_iso8601(date_to),
            },
        }

        response = client.post_json(
            "/invoices/query/metadata",
            query={
                "sortOrder": "Asc",
                "pageOffset": page_offset,
                "pageSize": DEFAULT_PAGE_SIZE,
            },
            json_body=filters,
            rate_group="invoiceMetadata",
        )

        invoices = response.get("invoices", [])
        if not isinstance(invoices, list):
            raise KsefApiError(
                f"Unexpected response shape: 'invoices' should be a list, got {type(invoices).__name__}"
            )

        all_invoices.extend(invoices)

        has_more = bool(response.get("hasMore"))
        is_truncated = bool(response.get("isTruncated"))

        if is_truncated:
            raise KsefApiError(
                "KSeF response is truncated (10,000 record technical limit reached). "
                "Use a narrower date range and repeat the query."
            )

        if not has_more:
            break

        page_offset += 1

    return all_invoices


def unique_ksef_numbers(invoices: Iterable[Dict[str, Any]]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []

    for invoice in invoices:
        ksef_number = invoice.get("ksefNumber")
        if isinstance(ksef_number, str) and ksef_number and ksef_number not in seen:
            seen.add(ksef_number)
            ordered.append(ksef_number)

    return ordered


def build_download_targets(
    invoices: Iterable[Dict[str, Any]],
    *,
    filename_mode: str,
    invoice_type: str,
) -> List[tuple[str, str]]:
    targets: List[tuple[str, str]] = []
    seen: set[str] = set()
    counterparty_field = "buyer" if invoice_type == "sales" else "seller"

    for invoice in invoices:
        ksef_number = invoice.get("ksefNumber")
        if not isinstance(ksef_number, str) or not ksef_number or ksef_number in seen:
            continue
        seen.add(ksef_number)

        if filename_mode == "seller-id":
            counterparty_name = ""
            counterparty = invoice.get(counterparty_field)
            if isinstance(counterparty, dict):
                name_raw = counterparty.get("name")
                if isinstance(name_raw, str):
                    counterparty_name = name_raw
            counterparty_prefix = sanitize_filename(counterparty_name)[:COUNTERPARTY_NAME_MAX_LEN]
            file_stem = f"{counterparty_prefix}__{ksef_number}" if counterparty_prefix else ksef_number
        else:
            file_stem = ksef_number

        targets.append((ksef_number, file_stem))

    return targets


def download_invoices(
    client: KsefClient,
    targets: Iterable[tuple[str, str]],
    output_dirs: List[Path],
    tracked_ids: set[str],
    *,
    overwrite: bool,
) -> tuple[int, int, List[tuple[Path, str]]]:
    downloaded = 0
    skipped_existing = 0
    downloaded_xml_files: List[tuple[Path, str]] = []

    for directory in output_dirs:
        directory.mkdir(parents=True, exist_ok=True)

    for ksef_number, file_stem in targets:
        if ksef_number in tracked_ids:
            continue

        safe_name = sanitize_filename(file_stem)
        targets_for_id = [directory / f"{safe_name}.xml" for directory in output_dirs]
        if all(target.exists() for target in targets_for_id) and not overwrite:
            skipped_existing += 1

        encoded = quote(ksef_number, safe="")
        xml_text = client.get_xml(f"/invoices/ksef/{encoded}", rate_group="invoiceDownload")
        for target in targets_for_id:
            if target.exists() and not overwrite:
                continue
            target.write_text(xml_text, encoding="utf-8", newline="\n")
        tracked_ids.add(ksef_number)
        for target in targets_for_id:
            if target.exists():
                downloaded_xml_files.append((target, ksef_number))
        downloaded += 1

    return downloaded, skipped_existing, downloaded_xml_files


def render_downloaded_xmls(xml_files: Iterable[tuple[Path, str]]) -> tuple[List[Path], List[str]]:
    try:
        from render_ksef_invoice_pdf import parse_invoice, render_invoice_pdf
    except Exception as exc:
        raise KsefApiError(f"Failed to import renderer module: {exc}") from exc

    rendered_paths: List[Path] = []
    failures: List[str] = []

    unique_xml_files: Dict[Path, str] = {}
    for xml_path, ksef_id in xml_files:
        unique_xml_files[xml_path] = ksef_id

    for xml_path in sorted(unique_xml_files, key=lambda p: str(p)):
        pdf_path = xml_path.with_suffix(".pdf")
        try:
            invoice = parse_invoice(xml_path)
            render_invoice_pdf(
                invoice,
                pdf_path,
                regular_font=None,
                bold_font=None,
                hide_empty_fields=False,
                ksef_id=unique_xml_files[xml_path],
            )
            rendered_paths.append(pdf_path)
        except Exception as exc:
            failures.append(f"{xml_path.name}: {exc}")

    return rendered_paths, failures


def main() -> int:
    args = parse_args()
    app_dir = get_app_dir()
    invoice_type_config = INVOICE_TYPE_CONFIG[args.invoice_type]
    subject_type = invoice_type_config["subject_type"]
    target_subdir = invoice_type_config["target_subdir"]
    invoice_type_label = invoice_type_config["label"]

    token_file_path = resolve_app_relative_path(args.token_file)
    dir_prefix_file = app_dir / MASTER_PREFIX_FILE
    try:
        ksef_token, token_source = load_ksef_token(token_file_path)
        context_value = resolve_context_nip(ksef_token)
    except (KsefApiError, OSError) as exc:
        console_error(str(exc))
        return 1

    try:
        start, end = month_range_for_selection(
            DEFAULT_TIMEZONE,
            year=args.year,
            month=args.month,
        )
    except Exception as exc:
        console_error(f"Invalid timezone '{DEFAULT_TIMEZONE}': {exc}")
        return 2

    month_dir_master = f"{start.year:04d}_{start.month:02d}"
    month_dir_local = f"{start.year:04d}-{start.month:02d}"

    master_prefix = load_master_prefix()
    if master_prefix is not None:
        output_dir = master_prefix / month_dir_master / target_subdir
        dir_prefix_message = (
            f"{MASTER_PREFIX_FILE} found: {dir_prefix_file.resolve()} -> {output_dir.resolve()}"
        )
    else:
        output_dir = app_dir / DEFAULT_LOCAL_OUT_DIR / month_dir_local / target_subdir
        if dir_prefix_file.exists():
            dir_prefix_message = (
                f"{MASTER_PREFIX_FILE} found but empty: {dir_prefix_file.resolve()}. "
                f"Using local target directory {output_dir.resolve()}"
            )
        else:
            dir_prefix_message = (
                f"{MASTER_PREFIX_FILE} not found next to the script or EXE. "
                f"Using local target directory {output_dir.resolve()}"
            )

    console_section("Configuration")
    if token_file_path.exists():
        console_ok(f"Token file found ({token_file_path.name}): {token_file_path.resolve()}")
    else:
        console_warn(f"Token file not found. Using token from {token_source}.")
    if master_prefix is not None:
        console_ok(dir_prefix_message)
    elif dir_prefix_file.exists():
        console_warn(dir_prefix_message)
    else:
        console_info(dir_prefix_message)
    console_info(f"Target directory: {output_dir.resolve()}")
    console_info(f"Date range: {to_iso8601(start)} -> {to_iso8601(end)}")
    console_info(f"Date type: {args.date_type}")
    console_info(f"Invoice type: {args.invoice_type} ({invoice_type_label})")
    console_info(f"Filename mode: {args.filename_mode}")
    console_info(f"Subject type: {subject_type}")
    console_info(f"Auth context: {AUTH_CONTEXT_TYPE}={context_value}")
    console_info(f"KSeF base URL: {DEFAULT_BASE_URL}")

    tracking_file = output_dir / TRACKING_FILE_NAME
    if tracking_file.exists():
        console_info(f"Tracking file found: {tracking_file.resolve()}")
    else:
        console_warn(
            f"Tracking file missing: {tracking_file.resolve()} -> all untracked invoices will be downloaded"
        )

    unauth_client = KsefClient(base_url=DEFAULT_BASE_URL)

    console_section("Authentication")
    try:
        auth_result = authenticate_by_ksef_token(
            unauth_client,
            ksef_token=ksef_token,
            context_value=context_value,
        )
    except (KsefApiError, ValueError) as exc:
        console_error(str(exc))
        return 1
    console_ok(f"Authentication succeeded. Reference: {auth_result.reference_number}")

    invoice_client = KsefClient(
        base_url=DEFAULT_BASE_URL,
        bearer_token=auth_result.access_token,
        refresh_token=auth_result.refresh_token,
        access_token_valid_until=auth_result.access_token_valid_until,
    )
    console_info(
        f"Access token valid until: "
        f"{auth_result.access_token_valid_until.isoformat(timespec='seconds')}"
    )

    console_section("Rate Limits")
    invoice_client.rate_limiters = load_rate_limiters(invoice_client)

    console_section("Query")
    try:
        metadata = fetch_all_metadata(
            invoice_client,
            subject_type=subject_type,
            date_type=args.date_type,
            date_from=start,
            date_to=end,
        )
    except (KsefApiError, ValueError) as exc:
        console_error(str(exc))
        return 1

    ksef_numbers = unique_ksef_numbers(metadata)
    download_targets = build_download_targets(
        metadata,
        filename_mode=args.filename_mode,
        invoice_type=args.invoice_type,
    )
    tracked_ids = load_tracking_ids(tracking_file)
    already_tracked = sum(1 for invoice_id, _ in download_targets if invoice_id in tracked_ids)

    console_ok(f"Metadata rows fetched: {len(metadata)}")
    console_info(f"Unique KSeF invoice numbers: {len(ksef_numbers)}")
    console_info(f"Tracked invoice IDs: {len(tracked_ids)}")
    console_info(f"Already tracked in this run: {already_tracked}")

    if args.dry_run:
        console_section("Results")
        console_warn("Dry run enabled. No files were downloaded or rendered.")
        console_list("Downloaded invoices", [], empty_message="none (dry run)")
        console_list("Rendered invoices", [], empty_message="none (dry run)")
        return 0

    try:
        downloaded, skipped, downloaded_xml_files = download_invoices(
            invoice_client,
            download_targets,
            [output_dir],
            tracked_ids,
            overwrite=args.overwrite,
        )
    except KsefApiError as exc:
        console_error(str(exc))
        return 1

    write_tracking_ids(tracking_file, tracked_ids)

    downloaded_name_map: Dict[Path, str] = {}
    for xml_path, ksef_id in downloaded_xml_files:
        downloaded_name_map[xml_path] = ksef_id
    downloaded_names = [path.name for path in sorted(downloaded_name_map, key=lambda p: str(p))]

    console_section("Results")
    console_ok(f"Downloaded XML files: {downloaded}")
    console_info(f"Skipped existing files: {skipped}")
    console_list(
        "Downloaded invoices",
        downloaded_names,
        empty_message="none",
    )

    if args.render == "yes":
        if downloaded_xml_files:
            try:
                rendered_paths, render_failures = render_downloaded_xmls(downloaded_xml_files)
            except KsefApiError as exc:
                console_error(str(exc))
                return 1
            console_ok(f"Rendered PDF files: {len(rendered_paths)}")
            console_list(
                "Rendered invoices",
                [path.name for path in rendered_paths],
                empty_message="none",
            )
            if render_failures:
                console_warn(f"Render failures: {len(render_failures)}")
                for failure in render_failures:
                    print(f"  - {colorize(failure, '33')}")
                return 1
        else:
            console_ok("Rendered PDF files: 0")
            console_list("Rendered invoices", [], empty_message="none")
    else:
        console_info("PDF rendering disabled (--render no).")
        console_list("Rendered invoices", [], empty_message="none (render disabled)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
