import logging
from datetime import timedelta
from typing import Any

from couchbase_operational_insights.cluster import Cluster
from couchbase_operational_insights.credential import Credential
from couchbase_operational_insights.options import (
    ClusterOptions,
    SecurityOptions,
    TimeoutOptions,
)

from ...servers.operational_insights.constants import (
    DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT,
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.utils.connection")


def connect_to_operational_insights_cluster(
    connection_string: str | None,
    username: str | None,
    password: str | None,
    ca_cert_path: str | None = None,
    client_cert_path: str | None = None,
    client_key_path: str | None = None,
    client_cert_password: str | None = None,
    handle_request_timeout_seconds: int = DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT,
) -> Cluster:
    """Connect to an Operational Insights cluster and return the cluster object.

    ``connection_string`` is an HTTP(S) URL — e.g. ``http://localhost:8095``
    for a local Operational Insights server, or ``https://<host>:18095`` for
    Capella — not a ``couchbase://`` connection string.

    The connection can be established using a client certificate (mTLS) or
    username/password. If ``client_cert_path`` is provided, it is used and
    ``username``/``password`` are ignored; otherwise username/password are
    required. ``client_cert_path`` may point at a PEM certificate (paired
    with ``client_key_path``) or a PKCS#12 bundle (``client_key_path`` left
    unset), matching ``Credential.from_certificate``. mTLS requires an
    ``https://`` endpoint — the SDK authenticates during the TLS handshake.
    ``ca_cert_path``, if given, is the CA/trust-store PEM used to verify the
    *server's* certificate and applies to either authentication mode.

    ``handle_request_timeout_seconds`` sets the SDK's ``handle_request_timeout``
    (SDK default 10s), which bounds every async-query handle request — start,
    status, fetch, discard and cancel. For ``fetch_results()`` it is a
    deadline on the whole call, row stream included, not a gap between
    reads: a result that takes longer than this to download fails part-way
    with "Unable to complete iteration. Request timed out." The SDK has no
    per-fetch timeout, so it is set cluster-wide; the other handle requests
    are quick, and only wait this long on a server that has stopped
    answering.

    Unlike the operational server's ``connect_to_couchbase_cluster``, the CLI
    options for this server are optional (so the server can start in "lazy",
    no-cluster mode for tool discovery), so the values are validated here
    rather than by Click's ``required=True``. If the connection fails, it
    will raise an exception.
    """
    if not connection_string:
        raise ValueError(
            "Missing Operational Insights credentials: connection_string. "
            "Set it via --connection-string or CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING. "
            "connection_string is an HTTP(S) URL (e.g. http://localhost:8095), "
            "not a couchbase:// string."
        )

    if client_cert_path:
        logger.debug("Using client certificate authentication")
        if not connection_string.startswith("https://"):
            raise ValueError(
                "Client certificate (mTLS) authentication requires an "
                "https:// Operational Insights endpoint; got "
                f"{connection_string!r}."
            )
        credential = Credential.from_certificate(
            client_cert_path, client_key_path, password=client_cert_password
        )
    else:
        logger.debug("Using username/password authentication")
        missing = [
            name
            for name, value in (("username", username), ("password", password))
            if not value
        ]
        if missing:
            raise ValueError(
                "Missing Operational Insights credentials: "
                f"{', '.join(missing)}. Set them via --username/--password or "
                "CB_OPERATIONAL_INSIGHTS_USERNAME/CB_OPERATIONAL_INSIGHTS_PASSWORD, or use --client-cert-path for "
                "certificate authentication."
            )
        credential = Credential.from_username_and_password(username, password)

    option_kwargs: dict[str, Any] = {
        "timeout_options": TimeoutOptions(
            handle_request_timeout=timedelta(seconds=handle_request_timeout_seconds)
        )
    }
    if ca_cert_path:
        option_kwargs["security_options"] = SecurityOptions(
            trust_only_pem_file=ca_cert_path
        )
    options = ClusterOptions(**option_kwargs)

    try:
        logger.info("Connecting to Operational Insights cluster...")
        cluster = Cluster.create_instance(connection_string, credential, options)
        logger.info("Successfully connected to Operational Insights cluster")
        return cluster
    except Exception as e:
        logger.error(
            "Failed to connect to Operational Insights cluster: %s. Verify the "
            "endpoint is an HTTP(S) URL (not a couchbase:// string) and the "
            "credentials are an Operational Insights user or a valid client "
            "certificate.",
            e,
            exc_info=True,
        )
        raise
