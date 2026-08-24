import asyncio
import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from spotify_to_tidal.webapp.tidal_auth import (
    TIDAL_AUDIT_SCOPES,
    TIDAL_OPENAPI_BASE_URL,
    TIDAL_TOKEN_URL,
    TidalAPIError,
    TidalCredentials,
    TidalOAuthError,
    TidalOpenAPIClient,
    build_authorization_url,
    exchange_authorization_code,
    generate_pkce_verifier,
    pkce_s256,
    _safe_next_url,
)


def test_pkce_generation_and_rfc7636_s256_vector():
    verifier = generate_pkce_verifier()

    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    assert pkce_s256(
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    ) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_authorization_url_uses_exact_minimal_public_scope():
    url = build_authorization_url(
        "client-id",
        "https://audit.example.test/auth/tidal/callback",
        "oauth-state",
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert TIDAL_AUDIT_SCOPES == ("collection.read",)
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.tidal.com"
    assert query["scope"] == ["collection.read"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["oauth-state"]
    assert "r_usr" not in parsed.query
    assert not any("write" in scope or scope == "w_usr" for scope in TIDAL_AUDIT_SCOPES)


def test_authorization_code_exchange_sends_pkce_and_read_only_scope():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={
            "access_token": "user-access-token",
            "refresh_token": "user-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "collection.read",
        })

    credentials = asyncio.run(exchange_authorization_code(
        client_id="client-id",
        client_secret="client-secret",
        code="one-time-code",
        code_verifier="pkce-verifier",
        redirect_uri="https://audit.example.test/auth/tidal/callback",
        transport=httpx.MockTransport(handler),
    ))
    request = captured["request"]
    form = parse_qs(request.content.decode())

    assert str(request.url) == TIDAL_TOKEN_URL
    assert request.method == "POST"
    assert form == {
        "client_id": ["client-id"],
        "client_secret": ["client-secret"],
        "code": ["one-time-code"],
        "code_verifier": ["pkce-verifier"],
        "grant_type": ["authorization_code"],
        "redirect_uri": ["https://audit.example.test/auth/tidal/callback"],
        "scope": ["collection.read"],
    }
    assert credentials.access_token == "user-access-token"
    assert credentials.refresh_token == "user-refresh-token"


def test_authorization_code_exchange_hides_oauth_error_details():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "invalid_grant",
            "access_token": "must-not-leak",
        })

    with pytest.raises(TidalOAuthError) as exc_info:
        asyncio.run(exchange_authorization_code(
            client_id="client-id",
            client_secret="client-secret",
            code="bad-code",
            code_verifier="pkce-verifier",
            redirect_uri="https://audit.example.test/auth/tidal/callback",
            transport=httpx.MockTransport(handler),
        ))

    message = str(exc_info.value)
    assert "invalid_grant" not in message
    assert "must-not-leak" not in message


def test_authorization_code_exchange_rejects_unexpected_write_scope():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "token",
            "scope": "collection.read collection.write",
        })

    with pytest.raises(TidalOAuthError, match="unexpected authorization scopes"):
        asyncio.run(exchange_authorization_code(
            client_id="client-id",
            client_secret="client-secret",
            code="one-time-code",
            code_verifier="pkce-verifier",
            redirect_uri="https://audit.example.test/auth/tidal/callback",
            transport=httpx.MockTransport(handler),
        ))


def test_openapi_favorites_uses_collection_relationship_and_only_read_requests():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/userCollectionTracks/me/relationships/items"):
            assert request.headers["Authorization"] == "Bearer user-access-token"
            if request.url.params.get("page[cursor]") == "second":
                return httpx.Response(200, json={
                    "data": [{
                        "id": "track-2",
                        "type": "tracks",
                        "meta": {"addedAt": "2022-06-01T08:15:00+02:00"},
                    }],
                    "links": {"next": None},
                })
            assert request.url.params.get("sort") == "addedAt"
            return httpx.Response(200, json={
                "data": [{
                    "id": "track-1",
                    "type": "tracks",
                    "meta": {"addedAt": "2021-05-12T14:32:10Z"},
                }],
                    "links": {
                    "next": (
                        "/userCollectionTracks/me/relationships/items"
                        "?page%5Bcursor%5D=second"
                    )
                },
            })
        if str(request.url) == TIDAL_TOKEN_URL:
            assert parse_qs(request.content.decode())["grant_type"] == ["client_credentials"]
            return httpx.Response(200, json={"access_token": "catalog-access-token"})
        if request.url.path.endswith("/tracks"):
            assert request.headers["Authorization"] == "Bearer catalog-access-token"
            assert request.url.params.get_list("filter[id]") == ["track-1", "track-2"]
            assert request.url.params.get("include") == "artists"
            return httpx.Response(200, json={
                "data": [
                    {
                        "id": "track-1",
                        "type": "tracks",
                        "attributes": {
                            "isrc": "ISRC1",
                            "title": "First track",
                            "duration": "PT3M1.5S",
                        },
                        "relationships": {
                            "artists": {"data": [{"id": "artist-1", "type": "artists"}]}
                        },
                    },
                    {
                        "id": "track-2",
                        "type": "tracks",
                        "attributes": {
                            "isrc": "ISRC2",
                            "title": "Second track",
                            "duration": "PT4M",
                            "version": "Live",
                        },
                        "relationships": {
                            "artists": {"data": [{"id": "artist-2", "type": "artists"}]}
                        },
                    },
                ],
                "included": [
                    {"id": "artist-1", "type": "artists", "attributes": {"name": "One"}},
                    {"id": "artist-2", "type": "artists", "attributes": {"name": "Two"}},
                ],
                "links": {},
            })
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    credentials = TidalCredentials(
        access_token="user-access-token",
        refresh_token="user-refresh-token",
        token_type="Bearer",
        expires_at=datetime.datetime.now(datetime.timezone.utc),
        scope="collection.read",
    )
    client = TidalOpenAPIClient(
        credentials,
        "client-id",
        "client-secret",
        transport=httpx.MockTransport(handler),
    )

    tracks = asyncio.run(client.favorite_tracks())

    assert [track.id for track in tracks] == ["track-1", "track-2"]
    assert tracks[0].date_added == datetime.datetime(
        2021, 5, 12, 14, 32, 10, tzinfo=datetime.timezone.utc
    )
    assert tracks[1].date_added == datetime.datetime(
        2022, 6, 1, 6, 15, tzinfo=datetime.timezone.utc
    )
    assert tracks[0].duration == 181.5
    assert tracks[0].artists[0].name == "One"
    assert tracks[1].version == "Live"
    provider_requests = [request for request in requests if "tidal.com" in request.url.host]
    assert all(
        request.method == "GET" or request.url.path == "/v1/oauth2/token"
        for request in provider_requests
    )
    assert not any(
        request.method in {"DELETE", "PATCH", "PUT"}
        or (
            request.method == "POST"
            and "userCollection" in request.url.path
        )
        for request in provider_requests
    )


@pytest.mark.parametrize(
    "unsafe_link",
    [
        "https://evil.example/v2/steal",
        "//evil.example/v2/steal",
        "https://openapi.tidal.com.evil.example/v2/steal",
        "https://openapi.tidal.com:444/v2/steal",
        "https://openapi.tidal.com/not-v2/steal",
    ],
)
def test_openapi_pagination_rejects_untrusted_destinations(unsafe_link):
    with pytest.raises(TidalAPIError, match="unsafe pagination link"):
        _safe_next_url(
            unsafe_link,
            f"{TIDAL_OPENAPI_BASE_URL}/userCollectionTracks/me/relationships/items",
        )


@pytest.mark.parametrize(
    ("failing_stage", "status", "expected_message"),
    [
        ("collection", 403, "Tidal collection page request failed (HTTP 403)"),
        ("catalog_token", 401, "Tidal catalog authorization failed (HTTP 401)"),
        ("catalog", 400, "Tidal catalog metadata request failed (HTTP 400)"),
    ],
)
def test_openapi_errors_identify_safe_stage_and_status(
    failing_stage,
    status,
    expected_message,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/userCollectionTracks/me/relationships/items"):
            if failing_stage == "collection":
                return httpx.Response(status, json={"secret": "not-disclosed"})
            return httpx.Response(200, json={
                "data": [{
                    "id": "track-1",
                    "type": "tracks",
                    "meta": {"addedAt": "2021-05-12T14:32:10Z"},
                }],
                "links": {"next": None},
            })
        if str(request.url) == TIDAL_TOKEN_URL:
            if failing_stage == "catalog_token":
                return httpx.Response(status, json={"secret": "not-disclosed"})
            return httpx.Response(200, json={"access_token": "catalog-token"})
        if request.url.path.endswith("/tracks") and failing_stage == "catalog":
            return httpx.Response(status, json={"secret": "not-disclosed"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = TidalOpenAPIClient(
        TidalCredentials(
            access_token="user-token",
            refresh_token=None,
            token_type="Bearer",
            expires_at=None,
            scope="collection.read",
        ),
        "client-id",
        "client-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TidalAPIError) as exc_info:
        asyncio.run(client.favorite_tracks())

    assert str(exc_info.value) == expected_message
    assert "not-disclosed" not in str(exc_info.value)
