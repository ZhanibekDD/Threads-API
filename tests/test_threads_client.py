from unittest.mock import AsyncMock

import pytest

from app.threads_client import DEFAULT_SEARCH_FIELDS, ThreadsClient


@pytest.mark.asyncio
async def test_keyword_search_requests_required_fields():
    client = ThreadsClient(access_token="token")
    client._request = AsyncMock(return_value={"data": []})
    try:
        await client.keyword_search("арест счета")
    finally:
        await client.aclose()

    client._request.assert_awaited_once_with(
        "GET",
        "/keyword_search",
        params={
            "q": "арест счета",
            "search_type": "RECENT",
            "limit": 25,
            "fields": DEFAULT_SEARCH_FIELDS,
        },
    )


@pytest.mark.asyncio
async def test_user_id_is_resolved_once_from_me():
    client = ThreadsClient(access_token="token", user_id="")
    client.get_me = AsyncMock(return_value={"id": "threads-user-1", "username": "zakonexpert"})
    try:
        first = await client._get_user_id()
        second = await client._get_user_id()
    finally:
        await client.aclose()

    assert first == "threads-user-1"
    assert second == "threads-user-1"
    client.get_me.assert_awaited_once()
