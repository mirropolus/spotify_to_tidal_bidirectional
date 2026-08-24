"""Read-only Tidal device authorization for the audit web application."""

from __future__ import annotations

import time

import tidalapi
from tidalapi.session import LinkLogin


class ReadOnlyTidalSession(tidalapi.Session):
    """Tidal session whose device grant requests only user-library read access."""

    oauth_scope = "r_usr"

    def get_link_login(self) -> LinkLogin:
        response = self.request_session.post(
            "https://auth.tidal.com/v1/oauth2/device_authorization",
            {
                "client_id": self.config.client_id,
                "scope": self.oauth_scope,
            },
        )
        if not response.ok:
            raise RuntimeError("Tidal device authorization was rejected")
        try:
            return LinkLogin(response.json())
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Tidal returned an invalid device authorization response") from exc

    def _check_link_login(self, link_login: LinkLogin, until_expiry: bool = True):
        remaining = link_login.expires_in if until_expiry else 1
        interval = max(1.0, link_login.interval)
        while remaining > 0:
            response = self.request_session.post(
                self.config.api_oauth2_token,
                {
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "device_code": link_login.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "scope": self.oauth_scope,
                },
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Tidal returned an invalid token response") from exc

            if response.ok:
                return payload

            error = payload.get("error") if isinstance(payload, dict) else None
            if error == "expired_token":
                break
            if error not in {"authorization_pending", "slow_down"}:
                raise RuntimeError("Tidal device authorization failed")
            if error == "slow_down":
                interval += 1
            time.sleep(interval)
            remaining -= interval

        raise TimeoutError("Tidal device authorization expired")


def new_read_only_tidal_session(client_id: str, client_secret: str) -> ReadOnlyTidalSession:
    """Create a Tidal session with operator-owned credentials and no disk cache."""
    session = ReadOnlyTidalSession()
    session.config.client_id = client_id
    session.config.client_secret = client_secret
    return session
