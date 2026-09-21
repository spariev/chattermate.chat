"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from app.core import cors as cors_module


def _redis_mock(cached_payload, pubsub):
    redis_client = MagicMock()
    redis_client.pubsub.return_value = pubsub
    redis_client.get.return_value = cached_payload
    return redis_client


def _pubsub_mock(messages):
    """get_message returns each entry in order; a CancelledError entry breaks
    the listener loop so the coroutine exits."""
    pubsub = MagicMock()
    pubsub.get_message.side_effect = messages
    return pubsub


@pytest.mark.asyncio
async def test_startup_merges_cached_origins_with_freshly_computed():
    """A stale cors:origins snapshot must not drop origins the process just
    computed from env/DB — regression test for the restart bug where editing
    CORS_ORIGINS had no effect until the Redis key was deleted."""
    cached = json.dumps({
        "origins": ["https://old.example.com"],
        "updated_at": 1.0,
    })
    pubsub = _pubsub_mock([asyncio.CancelledError()])
    redis_client = _redis_mock(cached, pubsub)

    with patch("app.core.redis.get_redis", return_value=redis_client), \
         patch.object(cors_module, "get_cors_origins",
                      return_value={"https://new.example.com"}), \
         patch.object(cors_module, "update_local_cors") as update:
        with pytest.raises(asyncio.CancelledError):
            await cors_module.listen_for_cors_updates(MagicMock())

    update.assert_called_once()
    applied = set(update.call_args[0][1])
    assert applied == {"https://old.example.com", "https://new.example.com"}


@pytest.mark.asyncio
async def test_pubsub_update_keeps_env_configured_origins():
    """A cors:update notification applies the published snapshot, but
    BASE_CORS_ORIGINS (env-configured) must never drop out of it."""
    payload = json.dumps({
        "origins": ["https://published.example.com"],
        "updated_at": 2.0,
    })
    pubsub = _pubsub_mock([
        {"type": "message", "channel": "cors:update", "data": "refresh"},
        asyncio.CancelledError(),
    ])
    redis_client = _redis_mock(payload, pubsub)

    with patch("app.core.redis.get_redis", return_value=redis_client), \
         patch.object(cors_module, "BASE_CORS_ORIGINS",
                      ["https://env.example.com"]), \
         patch.object(cors_module, "get_cors_origins",
                      return_value={"https://env.example.com"}), \
         patch.object(cors_module, "update_local_cors") as update:
        with pytest.raises(asyncio.CancelledError):
            await cors_module.listen_for_cors_updates(MagicMock())

    # Call 1 is the startup cache apply; call 2 is the published update.
    assert update.call_count == 2
    applied = set(update.call_args_list[-1][0][1])
    assert applied == {"https://published.example.com", "https://env.example.com"}


@pytest.mark.asyncio
async def test_startup_without_cached_origins_makes_no_update():
    """No cors:origins key -> nothing to apply on startup."""
    pubsub = _pubsub_mock([asyncio.CancelledError()])
    redis_client = _redis_mock(None, pubsub)

    with patch("app.core.redis.get_redis", return_value=redis_client), \
         patch.object(cors_module, "update_local_cors") as update:
        with pytest.raises(asyncio.CancelledError):
            await cors_module.listen_for_cors_updates(MagicMock())

    update.assert_not_called()


@pytest.mark.asyncio
async def test_startup_with_malformed_cached_origins_makes_no_update():
    """A corrupt cors:origins payload is ignored rather than crashing the
    listener."""
    pubsub = _pubsub_mock([asyncio.CancelledError()])
    redis_client = _redis_mock("not json", pubsub)

    with patch("app.core.redis.get_redis", return_value=redis_client), \
         patch.object(cors_module, "update_local_cors") as update:
        with pytest.raises(asyncio.CancelledError):
            await cors_module.listen_for_cors_updates(MagicMock())

    update.assert_not_called()
