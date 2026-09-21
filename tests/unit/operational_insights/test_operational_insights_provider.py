"""Unit tests for OperationalInsightsClusterProvider.

Covers the two things that differ from ``StaticClusterProvider``: teardown
calls ``shutdown()`` (not ``close()``), and ``get_configuration()`` never
returns a secret or one of the server-owned reserved keys.
"""

from unittest.mock import MagicMock, patch

from providers.operational_insights import OperationalInsightsClusterProvider

_SETTINGS = {
    "connection_string": "http://localhost:8095",
    "username": "Administrator",
    "password": "hunter2",
}


def _provider_with_cached_cluster():
    provider = OperationalInsightsClusterProvider(settings=_SETTINGS)
    cluster = MagicMock()
    with patch(
        "providers.operational_insights.connect_to_operational_insights_cluster",
        return_value=cluster,
    ):
        returned = provider.get_cluster(ctx=MagicMock())
    assert returned is cluster
    return provider, cluster


def test_get_cluster_connects_lazily_and_caches():
    provider = OperationalInsightsClusterProvider(settings=_SETTINGS)
    cluster = MagicMock()
    with patch(
        "providers.operational_insights.connect_to_operational_insights_cluster",
        return_value=cluster,
    ) as connect:
        first = provider.get_cluster(ctx=MagicMock())
        second = provider.get_cluster(ctx=MagicMock())

    connect.assert_called_once_with("http://localhost:8095", "Administrator", "hunter2")
    assert first is cluster
    assert second is cluster


def test_close_calls_shutdown_not_close():
    provider, cluster = _provider_with_cached_cluster()

    provider.close()

    cluster.shutdown.assert_called_once()
    cluster.close.assert_not_called()
    assert provider.is_connected(ctx=MagicMock()) is False


def test_close_before_connecting_is_a_no_op():
    provider = OperationalInsightsClusterProvider(settings=_SETTINGS)
    provider.close()  # must not raise


def test_get_configuration_omits_secrets_and_reserved_keys():
    provider = OperationalInsightsClusterProvider(settings=_SETTINGS)

    config = provider.get_configuration(ctx=MagicMock())

    assert config["connection_string"] == "http://localhost:8095"
    assert config["username"] == "Administrator"
    assert config["password_configured"] is True
    assert "password" not in config
    for reserved in (
        "read_only_mode",
        "disabled_tools",
        "confirmation_required_tools",
    ):
        assert reserved not in config


def test_get_configuration_reports_unset_password():
    provider = OperationalInsightsClusterProvider(
        settings={"connection_string": "http://localhost:8095", "username": "u"}
    )

    config = provider.get_configuration(ctx=MagicMock())

    assert config["password_configured"] is False


def test_is_connected_reflects_cache_state():
    provider = OperationalInsightsClusterProvider(settings=_SETTINGS)
    assert provider.is_connected(ctx=MagicMock()) is False

    provider, _ = _provider_with_cached_cluster()
    assert provider.is_connected(ctx=MagicMock()) is True
