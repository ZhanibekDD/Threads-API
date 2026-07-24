from unittest.mock import AsyncMock

import pytest

from app.threads_client import DEFAULT_SEARCH_FIELDS, ThreadsClient


@pytest.mark.asyncio
async def test_keyword_search_requests_required_fields():
    client = ThreadsClient(access_token="token")
    client._request = AsyncMock(return_value={"data": []})
    try:
        await client.keyword_search("арестовали Kaspi")
    finally:
        await client.aclose()

    _, path = client._request.call_args.args
    params = client._request.call_args.kwargs["params"]
    assert path == "/keyword_search"
    assert params["fields"] == DEFAULT_SEARCH_FIELDS
    assert "text" in params["fields"]
    assert "timestamp" in params["fields"]


@pytest.mark.asyncio
async def test_user_id_is_resolved_from_me_once():
    client = ThreadsClient(access_token="token", user_id="")
    client.get_me = AsyncMock(return_value={"id": "user-123", "username": "u"})

    first = await client._get_user_id()
    second = await client._get_user_id()
    await client.aclose()

    assert first == "user-123"
    assert second == "user-123"
    client.get_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_own_post_uses_resolved_user_id():
    client = ThreadsClient(access_token="token", user_id="user-123")
    client._request = AsyncMock(
        side_effect=[{"id": "container-1"}, {"id": "post-1"}]
    )
    try:
        result = await client.create_own_post("text")
    finally:
        await client.aclose()

    assert result == "post-1"
    first_path = client._request.call_args_list[0].args[1]
    assert first_path == "/user-123/threads"
