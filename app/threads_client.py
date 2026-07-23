"""Thin client around the official Meta Threads API (graph.threads.net).

Only uses documented endpoints — no scraping, no password login. Requires
scopes: threads_basic, threads_keyword_search, threads_manage_replies,
threads_read_replies, threads_manage_mentions, threads_content_publish.

Endpoint names follow the public Threads API docs as of the API version
configured in THREADS_API_VERSION; verify against
https://developers.facebook.com/docs/threads before going live, Meta
occasionally revises paths/params.
"""

import asyncio
import logging
import time

import httpx

from app.config import config

logger = logging.getLogger("threads_client")

GRAPH_BASE = "https://graph.threads.net"


class ThreadsAPIError(Exception):
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Threads API error {status_code}: {payload}")


class RateLimitError(ThreadsAPIError):
    pass


class ThreadsClient:
    def __init__(self, access_token: str | None = None, api_version: str | None = None):
        self.access_token = access_token or config.threads_access_token
        self.api_version = api_version or config.threads_api_version
        self._client = httpx.AsyncClient(base_url=GRAPH_BASE, timeout=20.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                        json: dict | None = None, data: dict | None = None,
                        include_token: bool = True, max_retries: int = 4) -> dict:
        params = dict(params or {})
        if include_token and self.access_token:
            params.setdefault("access_token", self.access_token)

        attempt = 0
        while True:
            attempt += 1
            resp = await self._client.request(method, path, params=params, json=json, data=data)
            if resp.status_code == 200:
                return resp.json()

            payload = {}
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw": resp.text}

            is_rate_limited = resp.status_code == 429 or (
                resp.status_code == 400
                and str(payload.get("error", {}).get("code")) in ("4", "17", "32", "613")
            )

            if is_rate_limited and attempt <= max_retries:
                delay = min(2 ** attempt, 60)
                logger.warning("threads_rate_limited attempt=%s delay=%s", attempt, delay)
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 500 and attempt <= max_retries:
                delay = min(2 ** attempt, 30)
                logger.warning("threads_server_error status=%s attempt=%s delay=%s",
                                resp.status_code, attempt, delay)
                await asyncio.sleep(delay)
                continue

            logger.error("threads_api_error status=%s", resp.status_code)
            raise ThreadsAPIError(resp.status_code, payload)

    # -- OAuth / token lifecycle -------------------------------------------------

    async def exchange_code_for_token(self, code: str) -> dict:
        """Sent as a form-encoded POST body (not query params) so the
        one-time authorization `code` never ends up in a logged request URL.
        Uses `include_token=False` since there is no access token yet at
        this point in the flow — the app credentials are what authenticate
        this specific call."""
        return await self._request(
            "POST",
            "/oauth/access_token",
            include_token=False,
            data={
                "client_id": config.threads_app_id,
                "client_secret": config.threads_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": config.threads_redirect_uri,
                "code": code,
            },
        )

    async def exchange_for_long_lived_token(self, short_lived_token: str) -> dict:
        return await self._request(
            "GET",
            "/access_token",
            include_token=False,
            params={
                "grant_type": "th_exchange_token",
                "client_secret": config.threads_app_secret,
                "access_token": short_lived_token,
            },
        )

    async def refresh_long_lived_token(self) -> dict:
        """Refresh the current long-lived token. Call this well before the
        ~60 day expiry (e.g. daily) and persist the new token — never log it."""
        return await self._request(
            "GET",
            "/refresh_access_token",
            include_token=False,
            params={"grant_type": "th_refresh_token", "access_token": self.access_token},
        )

    # -- Search / discovery -------------------------------------------------

    async def keyword_search(self, query: str, search_type: str = "RECENT",
                              limit: int = 25, after: str | None = None) -> dict:
        params = {"q": query, "search_type": search_type, "limit": limit}
        if after:
            params["after"] = after
        return await self._request("GET", "/keyword_search", params=params)

    async def get_me(self, fields: str = "id,username") -> dict:
        return await self._request("GET", "/me", params={"fields": fields})

    async def get_mentions(self, fields: str = "id,text,username,permalink,timestamp") -> dict:
        return await self._request(
            "GET", f"/{config.threads_user_id}/mentions", params={"fields": fields}
        )

    async def get_replies(self, media_id: str,
                           fields: str = "id,text,username,permalink,timestamp") -> dict:
        return await self._request("GET", f"/{media_id}/replies", params={"fields": fields})

    # -- Publishing -----------------------------------------------------------

    async def create_reply_container(self, text: str, reply_to_id: str) -> dict:
        return await self._request(
            "POST",
            f"/{config.threads_user_id}/threads",
            params={"media_type": "TEXT", "text": text, "reply_to_id": reply_to_id},
        )

    async def publish_container(self, creation_id: str) -> dict:
        return await self._request(
            "POST",
            f"/{config.threads_user_id}/threads_publish",
            params={"creation_id": creation_id},
        )

    async def publish_reply(self, text: str, reply_to_id: str) -> str:
        """Two-step publish (create container, then publish it). Returns the
        published reply's id. Callers MUST only invoke this after an explicit
        human approval — see pipeline.publish_approved_reply()."""
        container = await self.create_reply_container(text, reply_to_id)
        creation_id = container["id"]
        # Threads recommends a short pause between container creation and publish.
        await asyncio.sleep(2)
        published = await self.publish_container(creation_id)
        return published["id"]

    async def delete_post(self, media_id: str) -> dict:
        return await self._request("DELETE", f"/{media_id}")

    async def create_own_post(self, text: str) -> str:
        container = await self._request(
            "POST",
            f"/{config.threads_user_id}/threads",
            params={"media_type": "TEXT", "text": text},
        )
        await asyncio.sleep(2)
        published = await self.publish_container(container["id"])
        return published["id"]
