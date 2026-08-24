"""Official, read-only Tidal OAuth and OpenAPI support for the web audit."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import datetime
from email.utils import parsedate_to_datetime
import hashlib
import math
import random
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx


TIDAL_AUTHORIZE_URL = "https://login.tidal.com/authorize"
TIDAL_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_OPENAPI_BASE_URL = "https://openapi.tidal.com/v2"
TIDAL_AUDIT_SCOPES = ("collection.read",)
TIDAL_STATUS_MAX_RETRIES = 3
TIDAL_STATUS_RETRY_BASE_SECONDS = 0.5
TIDAL_STATUS_RETRY_MAX_SECONDS = 16.0
TIDAL_RETRY_AFTER_MAX_WAIT_SECONDS = 60.0
_JSON_API_MEDIA_TYPE = "application/vnd.api+json"
_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


class TidalOAuthError(RuntimeError):
    """A Tidal OAuth request failed without exposing its sensitive response."""


class TidalAPIError(RuntimeError):
    """A read-only Tidal OpenAPI request failed."""


@dataclass(frozen=True)
class TidalCredentials:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: datetime.datetime | None
    scope: str


@dataclass(frozen=True)
class TidalArtist:
    name: str


@dataclass(frozen=True)
class TidalAuditTrack:
    """Small adapter matching the fields used by the existing audit matcher."""

    id: str
    isrc: str
    name: str
    duration: float
    artists: tuple[TidalArtist, ...]
    version: str | None
    date_added: datetime.datetime | None
    user_date_added: datetime.datetime | None


def generate_pkce_verifier() -> str:
    """Generate an RFC 7636 verifier in the allowed 43-128 character range."""
    return secrets.token_urlsafe(64)


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    code_verifier: str,
) -> str:
    query = urlencode({
        "client_id": client_id,
        "code_challenge": pkce_s256(code_verifier),
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(TIDAL_AUDIT_SCOPES),
        "state": state,
    })
    return f"{TIDAL_AUTHORIZE_URL}?{query}"


def _credentials_from_payload(payload: Any) -> TidalCredentials:
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise TidalOAuthError("Tidal returned an invalid token response")
    returned_scope = payload.get("scope")
    if isinstance(returned_scope, str) and set(returned_scope.split()) != set(TIDAL_AUDIT_SCOPES):
        raise TidalOAuthError("Tidal returned unexpected authorization scopes")
    expires_at = None
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=float(expires_in)
        )
    return TidalCredentials(
        access_token=payload["access_token"],
        refresh_token=(
            payload.get("refresh_token")
            if isinstance(payload.get("refresh_token"), str)
            else None
        ),
        token_type=(payload.get("token_type") or "Bearer"),
        expires_at=expires_at,
        scope=(returned_scope or " ".join(TIDAL_AUDIT_SCOPES)),
    )


async def exchange_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TidalCredentials:
    """Exchange one authorization code; response details are never put in errors."""
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            response = await client.post(
                TIDAL_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "scope": " ".join(TIDAL_AUDIT_SCOPES),
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return _credentials_from_payload(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise TidalOAuthError("Tidal rejected the authorization code exchange") from exc


def _parse_datetime(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _duration_seconds(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        return 0.0
    parts = {key: float(number or 0) for key, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _retry_after_seconds(
    value: str | None,
    *,
    now: datetime.datetime | None = None,
) -> float | None:
    """Parse Retry-After delay-seconds or HTTP-date without reading a body."""
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return float(stripped)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (retry_at - current.astimezone(datetime.timezone.utc)).total_seconds())


def _safe_next_url(value: Any, current_url: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TidalAPIError("Tidal returned an invalid pagination link")

    parsed_value = urlparse(value)
    if parsed_value.scheme or parsed_value.netloc:
        candidate = value
    elif value.startswith("//"):
        raise TidalAPIError("Tidal returned an unsafe pagination link")
    elif value.startswith("?"):
        candidate = urljoin(current_url, value)
    elif value.startswith("/v2/"):
        candidate = f"https://openapi.tidal.com{value}"
    else:
        candidate = f"{TIDAL_OPENAPI_BASE_URL}/{value.lstrip('/')}"

    parsed = urlparse(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise TidalAPIError("Tidal returned an unsafe pagination link") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "openapi.tidal.com"
        or port not in {None, 443}
        or not parsed.path.startswith("/v2/")
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise TidalAPIError("Tidal returned an unsafe pagination link")
    return candidate


class TidalOpenAPIClient:
    """Read only My Collection client; it intentionally exposes no write methods."""

    def __init__(
        self,
        credentials: TidalCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport = transport

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        stage: str,
        params: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Retry only read-only requests using TIDAL SDK-style status backoff."""
        retries = 0
        while True:
            response = await client.get(url, params=params, headers=headers)
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if not retryable or retries >= TIDAL_STATUS_MAX_RETRIES:
                return response

            jitter = 0.8 + random.random() * 0.2
            backoff = min(
                TIDAL_STATUS_RETRY_BASE_SECONDS * (2 ** retries) * jitter,
                TIDAL_STATUS_RETRY_MAX_SECONDS,
            )
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            if (
                response.status_code == 429
                and retry_after is not None
                and retry_after > TIDAL_RETRY_AFTER_MAX_WAIT_SECONDS
            ):
                wait_seconds = math.ceil(retry_after)
                raise TidalAPIError(
                    f"{stage} was rate limited; try again in {wait_seconds} seconds"
                )
            await asyncio.sleep(max(backoff, retry_after or 0.0))
            retries += 1

    async def favorite_tracks(self) -> list[TidalAuditTrack]:
        """Load favorites and their historical addedAt using official OpenAPI GETs."""
        collection: list[tuple[str, datetime.datetime | None]] = []
        resources: dict[str, dict] = {}
        artists: dict[str, str] = {}
        user_headers = {
            "Authorization": f"Bearer {self._credentials.access_token}",
            "Accept": _JSON_API_MEDIA_TYPE,
        }
        url: str | None = (
            f"{TIDAL_OPENAPI_BASE_URL}/userCollectionTracks/me/relationships/items"
        )
        params: dict[str, str] | None = {
            "sort": "addedAt",
            "include": "items.artists",
        }

        stage = "Tidal collection page request"
        try:
            async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
                while url:
                    current_url = url
                    response = await self._get_with_retry(
                        client,
                        url,
                        stage=stage,
                        params=params,
                        headers=user_headers,
                    )
                    if response.is_error:
                        retry_suffix = (
                            f" after {TIDAL_STATUS_MAX_RETRIES} retries"
                            if response.status_code == 429
                            or 500 <= response.status_code < 600
                            else ""
                        )
                        raise TidalAPIError(
                            f"{stage} failed{retry_suffix} (HTTP {response.status_code})"
                        )
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                        raise TidalAPIError("Tidal returned an invalid collection response")
                    for item in payload["data"]:
                        if not isinstance(item, dict) or not item.get("id"):
                            raise TidalAPIError("Tidal returned an invalid collection item")
                        collection.append((
                            str(item["id"]),
                            _parse_datetime((item.get("meta") or {}).get("addedAt")),
                        ))
                    included_resources = payload.get("included") or []
                    if not isinstance(included_resources, list):
                        raise TidalAPIError("Tidal returned invalid included metadata")
                    for included in included_resources:
                        if not isinstance(included, dict) or not included.get("id"):
                            raise TidalAPIError("Tidal returned invalid included metadata")
                        included_id = str(included["id"])
                        if included.get("type") == "tracks":
                            resources[included_id] = included
                        elif included.get("type") == "artists":
                            name = (included.get("attributes") or {}).get("name")
                            if isinstance(name, str):
                                artists[included_id] = name
                    url = _safe_next_url(
                        (payload.get("links") or {}).get("next"),
                        current_url,
                    )
                    params = None

                if not collection:
                    return []
        except TidalAPIError:
            raise
        except httpx.RequestError as exc:
            raise TidalAPIError(f"{stage} could not reach Tidal") from exc
        except ValueError as exc:
            raise TidalAPIError(f"{stage} returned invalid JSON") from exc

        tracks: list[TidalAuditTrack] = []
        for track_id, added_at in collection:
            resource = resources.get(track_id)
            if resource is None:
                raise TidalAPIError("Tidal collection metadata was incomplete")
            attributes = resource.get("attributes") or {}
            artist_ids = (
                ((resource.get("relationships") or {}).get("artists") or {}).get("data")
                or []
            )
            track_artists = tuple(
                TidalArtist(artists[str(identifier["id"])])
                for identifier in artist_ids
                if isinstance(identifier, dict) and str(identifier.get("id")) in artists
            )
            tracks.append(TidalAuditTrack(
                id=track_id,
                isrc=str(attributes.get("isrc") or ""),
                name=str(attributes.get("title") or ""),
                duration=_duration_seconds(attributes.get("duration")),
                artists=track_artists,
                version=(str(attributes["version"]) if attributes.get("version") else None),
                date_added=added_at,
                user_date_added=added_at,
            ))
        return tracks
