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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_BASE_URL = "https://api.ksef.mf.gov.pl/v2"
DEFAULT_SUBJECT_TYPE = "Subject2"
DEFAULT_DATE_TYPE = "PermanentStorage"
DEFAULT_PAGE_SIZE = 100
DEFAULT_CONTEXT_TYPE = "Nip"
DEFAULT_AUTH_POLL_INTERVAL = 1.0
DEFAULT_AUTH_TIMEOUT_SECONDS = 120
DEFAULT_FILENAME_MODE = "id"
DEFAULT_RENDER_MODE = "yes"
SELLER_NAME_MAX_LEN = 15
TRACKING_FILE_NAME = "downloaded.txt"
MASTER_PREFIX_FILE = "dir_prefix.txt"


class KsefApiError(RuntimeError):
    """Raised when KSeF API returns a non-success response."""


@dataclass
class AuthResult:
    access_token: str
    refresh_token: str
    reference_number: str


@dataclass
class KsefClient:
    base_url: str
    bearer_token: Optional[str] = None
    timeout_seconds: int = 60

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        accept: str = "application/json",
        bearer_token: Optional[str] = None,
    ) -> bytes:
        base = self.base_url.rstrip("/")
        url = f"{base}/{path.lstrip('/')}"

        if query:
            query_string = urlencode({k: v for k, v in query.items() if v is not None})
            url = f"{url}?{query_string}"

        data: Optional[bytes] = None
        headers = {"Accept": accept}

        effective_bearer = bearer_token if bearer_token is not None else self.bearer_token
        if effective_bearer:
            headers["Authorization"] = f"Bearer {effective_bearer}"

        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url=url, method=method.upper(), data=data, headers=headers)

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise KsefApiError(
                f"KSeF API error {exc.code} for {method.upper()} {url}: {body}"
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
    ) -> Dict[str, Any]:
        payload = self._request(
            "POST",
            path,
            query=query,
            json_body=json_body,
            accept="application/json",
            bearer_token=bearer_token,
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
    ) -> Any:
        payload = self._request(
            "GET",
            path,
            query=query,
            accept="application/json",
            bearer_token=bearer_token,
        )
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise KsefApiError(
                f"Invalid JSON in response for GET {path}: {payload[:500]!r}"
            ) from exc

    def get_xml(self, path: str, *, bearer_token: Optional[str] = None) -> str:
        payload = self._request(
            "GET",
            path,
            accept="application/xml",
            bearer_token=bearer_token,
        )
        return payload.decode("utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate with KSeF token and download current-month invoices."
    )
    parser.add_argument(
        "--token-file",
        default="token.txt",
        help="Path to KSeF token file (default: token.txt).",
    )
    parser.add_argument(
        "--context-type",
        default=DEFAULT_CONTEXT_TYPE,
        choices=["Nip", "InternalId", "NipVatUe", "PeppolId"],
        help="Context identifier type for /auth/ksef-token.",
    )
    parser.add_argument(
        "--context-value",
        default=None,
        help="Context identifier value. If omitted and context-type=Nip, tries to infer from token.",
    )
    parser.add_argument(
        "--auth-poll-interval",
        type=float,
        default=DEFAULT_AUTH_POLL_INTERVAL,
        help=f"Polling interval in seconds for auth status (default: {DEFAULT_AUTH_POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--auth-timeout-seconds",
        type=int,
        default=DEFAULT_AUTH_TIMEOUT_SECONDS,
        help=f"Maximum wait for auth completion (default: {DEFAULT_AUTH_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--subject-type",
        default=DEFAULT_SUBJECT_TYPE,
        choices=["Subject2"],
        help="Fixed to Subject2: purchase invoices (you as buyer).",
    )
    parser.add_argument(
        "--date-type",
        default=DEFAULT_DATE_TYPE,
        choices=["Issue", "Invoicing", "PermanentStorage"],
        help="Date dimension used in query filters.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="Metadata page size (10-250, default 100).",
    )
    parser.add_argument(
        "--out-dir",
        default="downloads",
        help="Directory where invoice XML files will be saved.",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/Warsaw",
        help="Timezone used to compute the current month window (default: Europe/Warsaw).",
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
            "'seller-id' -> <sellerNamePrefix15>__<ksefNumber>.xml"
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
    prefix_file = Path(MASTER_PREFIX_FILE)
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
        path = (Path.cwd() / path).resolve()
    return path


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


def resolve_context_value(
    context_type: str,
    provided_value: Optional[str],
    ksef_token: str,
) -> str:
    if provided_value:
        return provided_value.strip()

    if context_type == "Nip":
        inferred_nip = infer_nip_from_token(ksef_token)
        if inferred_nip:
            return inferred_nip

    raise KsefApiError(
        "Missing context value. Provide --context-value (or KSEF token with embedded 'nip-XXXXXXXXXX')."
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


def sanitize_filename(name: str) -> str:
    # Keep only characters safe on Windows/Linux filesystems.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
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
    context_type: str,
    context_value: str,
    poll_interval_seconds: float,
    timeout_seconds: int,
) -> AuthResult:
    if poll_interval_seconds <= 0:
        raise ValueError("auth-poll-interval must be > 0")
    if timeout_seconds <= 0:
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
                "type": context_type,
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

    deadline = time.monotonic() + timeout_seconds

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
                    f"Authentication timed out after {timeout_seconds}s (reference {reference_number})."
                )
            time.sleep(poll_interval_seconds)
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

    if not isinstance(access_token, str) or not access_token:
        raise KsefApiError("Invalid /auth/token/redeem response: missing accessToken.token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise KsefApiError("Invalid /auth/token/redeem response: missing refreshToken.token")

    return AuthResult(
        access_token=access_token,
        refresh_token=refresh_token,
        reference_number=reference_number,
    )


def fetch_all_metadata(
    client: KsefClient,
    *,
    subject_type: str,
    date_type: str,
    date_from: datetime,
    date_to: datetime,
    page_size: int,
) -> List[Dict[str, Any]]:
    if page_size < 10 or page_size > 250:
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
                "pageSize": page_size,
            },
            json_body=filters,
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
) -> List[tuple[str, str]]:
    targets: List[tuple[str, str]] = []
    seen: set[str] = set()

    for invoice in invoices:
        ksef_number = invoice.get("ksefNumber")
        if not isinstance(ksef_number, str) or not ksef_number or ksef_number in seen:
            continue
        seen.add(ksef_number)

        if filename_mode == "seller-id":
            seller_name = ""
            seller = invoice.get("seller")
            if isinstance(seller, dict):
                name_raw = seller.get("name")
                if isinstance(name_raw, str):
                    seller_name = name_raw
            seller_prefix = sanitize_filename(seller_name)[:SELLER_NAME_MAX_LEN]
            file_stem = f"{seller_prefix}__{ksef_number}" if seller_prefix else ksef_number
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
) -> tuple[int, int, List[Path]]:
    downloaded = 0
    skipped_existing = 0
    downloaded_xml_paths: List[Path] = []

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
        xml_text = client.get_xml(f"/invoices/ksef/{encoded}")
        for target in targets_for_id:
            if target.exists() and not overwrite:
                continue
            target.write_text(xml_text, encoding="utf-8", newline="\n")
        tracked_ids.add(ksef_number)
        for target in targets_for_id:
            if target.exists():
                downloaded_xml_paths.append(target)
        downloaded += 1

    return downloaded, skipped_existing, downloaded_xml_paths


def render_downloaded_xmls(xml_paths: Iterable[Path]) -> tuple[int, int]:
    try:
        from render_ksef_invoice_pdf import parse_invoice, render_invoice_pdf
    except Exception as exc:
        raise KsefApiError(f"Failed to import renderer module: {exc}") from exc

    rendered = 0
    failed = 0

    unique_xml_paths = sorted(set(xml_paths), key=lambda p: str(p))
    for xml_path in unique_xml_paths:
        pdf_path = xml_path.with_suffix(".pdf")
        try:
            invoice = parse_invoice(xml_path)
            render_invoice_pdf(
                invoice,
                pdf_path,
                regular_font=None,
                bold_font=None,
                hide_empty_fields=False,
            )
            rendered += 1
        except Exception as exc:
            failed += 1
            print(f"Render failed for {xml_path}: {exc}", file=sys.stderr)

    return rendered, failed


def main() -> int:
    args = parse_args()

    token_file_path = Path(args.token_file)
    try:
        ksef_token, token_source = load_ksef_token(token_file_path)
        context_value = resolve_context_value(args.context_type, args.context_value, ksef_token)
    except (KsefApiError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        start, end = month_range_for_selection(
            args.timezone,
            year=args.year,
            month=args.month,
        )
    except Exception as exc:
        print(f"Invalid timezone '{args.timezone}': {exc}", file=sys.stderr)
        return 2

    unauth_client = KsefClient(base_url=DEFAULT_BASE_URL)

    print(f"KSeF base URL: {DEFAULT_BASE_URL}")
    print(f"Token source: {token_source}")
    print(f"Auth context: {args.context_type}={context_value}")
    print(f"Subject type: {args.subject_type}")
    print(f"Date type: {args.date_type}")
    print(f"Current month window: {to_iso8601(start)} -> {to_iso8601(end)}")

    try:
        auth_result = authenticate_by_ksef_token(
            unauth_client,
            ksef_token=ksef_token,
            context_type=args.context_type,
            context_value=context_value,
            poll_interval_seconds=args.auth_poll_interval,
            timeout_seconds=args.auth_timeout_seconds,
        )
    except (KsefApiError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Authentication succeeded. Reference: {auth_result.reference_number}")

    invoice_client = KsefClient(base_url=DEFAULT_BASE_URL, bearer_token=auth_result.access_token)

    try:
        metadata = fetch_all_metadata(
            invoice_client,
            subject_type=args.subject_type,
            date_type=args.date_type,
            date_from=start,
            date_to=end,
            page_size=args.page_size,
        )
    except (KsefApiError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Metadata rows fetched: {len(metadata)}")

    ksef_numbers = unique_ksef_numbers(metadata)
    print(f"Unique KSeF invoice numbers: {len(ksef_numbers)}")
    print(f"Filename mode: {args.filename_mode}")

    download_targets = build_download_targets(
        metadata,
        filename_mode=args.filename_mode,
    )

    month_dir_local = f"{start.year:04d}-{start.month:02d}"
    month_dir_master = f"{start.year:04d}_{start.month:02d}"
    output_dir = Path(args.out_dir) / month_dir_local

    master_prefix = load_master_prefix()
    mirror_dir: Optional[Path] = None
    if master_prefix is not None:
        mirror_dir = master_prefix / month_dir_master / "ksef"

    output_dirs = [output_dir]
    if mirror_dir is not None:
        output_dirs.append(mirror_dir)

    # If mirror is configured, treat mirror tracking file as authoritative source.
    tracking_file_primary = (mirror_dir / TRACKING_FILE_NAME) if mirror_dir is not None else (output_dir / TRACKING_FILE_NAME)
    tracking_files_to_write = [directory / TRACKING_FILE_NAME for directory in output_dirs]
    tracked_ids = load_tracking_ids(tracking_file_primary)
    already_tracked = sum(1 for invoice_id, _ in download_targets if invoice_id in tracked_ids)

    if tracking_file_primary.exists():
        print(f"Tracking file source: {tracking_file_primary.resolve()}")
    else:
        print(f"Tracking file source: {tracking_file_primary.resolve()} (missing -> full redownload of untracked IDs)")

    print(f"Tracked invoice IDs: {len(tracked_ids)}")
    print(f"Already tracked in this run: {already_tracked}")
    if mirror_dir is not None:
        print(f"Mirror output directory: {mirror_dir.resolve()}")
    else:
        print(f"Mirror output directory: not configured ({MASTER_PREFIX_FILE} missing/empty)")

    if args.dry_run:
        return 0

    try:
        downloaded, skipped, downloaded_xml_paths = download_invoices(
            invoice_client,
            download_targets,
            output_dirs,
            tracked_ids,
            overwrite=args.overwrite,
        )
    except KsefApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for track_file in tracking_files_to_write:
        write_tracking_ids(track_file, tracked_ids)

    print(f"Downloaded XML files: {downloaded}")
    print(f"Skipped existing files: {skipped}")
    print(f"Output directory: {output_dir.resolve()}")

    if args.render == "yes":
        if downloaded_xml_paths:
            try:
                rendered, render_failed = render_downloaded_xmls(downloaded_xml_paths)
            except KsefApiError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Rendered PDF files: {rendered}")
            print(f"Render failures: {render_failed}")
            if render_failed > 0:
                return 1
        else:
            print("Rendered PDF files: 0")
            print("Render failures: 0")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
