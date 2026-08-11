import logging
import os
from datetime import timedelta

from couchbase.auth import CertificateAuthenticator, PasswordAuthenticator
from couchbase.bucket import Bucket
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions
from couchbase_analytics.cluster import Cluster as AnalyticsCluster
from couchbase_analytics.credential import Credential as AnalyticsCredential
from couchbase_analytics.options import ClusterOptions as AnalyticsClusterOptions
from couchbase_analytics.options import SecurityOptions as AnalyticsSecurityOptions

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.connection")


def connect_to_couchbase_cluster(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
    client_cert_path: str | None = None,
    client_key_path: str | None = None,
) -> Cluster:
    """Connect to Couchbase cluster and return the cluster object if successful.
    The connection can be established using the client certificate and key or the username and password. Optionally, the CA root certificate path can also be provided.
    Either of the path to the client certificate and key or the username and password should be provided.
    If the client certificate and key are provided, the username and password are not used.
    If both the client certificate and key and the username and password are provided, the client certificate is used for authentication.
    If the connection fails, it will raise an exception.
    """

    try:
        logger.info("Connecting to Couchbase cluster...")
        if client_cert_path and client_key_path:
            logger.debug("Using client certificate authentication")
            if not os.path.exists(client_cert_path) or not os.path.exists(
                client_key_path
            ):
                raise FileNotFoundError(
                    f"Client certificate files not found at {os.path.basename(client_cert_path)} or {os.path.basename(client_key_path)}."
                )

            auth = CertificateAuthenticator(
                cert_path=client_cert_path,
                key_path=client_key_path,
                trust_store_path=ca_cert_path,
            )
        elif client_cert_path or client_key_path:
            raise ValueError(
                "Both client_cert_path and client_key_path must be provided together "
                "for certificate authentication; only one was set."
            )
        else:
            logger.debug("Using username/password authentication")
            auth = PasswordAuthenticator(username, password, cert_path=ca_cert_path)
        options = ClusterOptions(auth)
        options.apply_profile("wan_development")

        cluster = Cluster(connection_string, options)  # type: ignore
        cluster.wait_until_ready(timedelta(seconds=5))

        logger.info("Successfully connected to Couchbase cluster")
        return cluster
    except Exception as e:
        logger.error(f"Failed to connect to Couchbase cluster: {e}", exc_info=True)
        raise


def connect_to_analytics_cluster(
    connection_string: str,
    username: str,
    password: str,
    ca_cert_path: str | None = None,
) -> AnalyticsCluster:
    """Connect to a Couchbase Enterprise Analytics (EA) cluster.

    EA is reached through the separate `couchbase_analytics` SDK package, not
    `couchbase` — the `AnalyticsCluster` returned here is a distinct class
    from the operational `couchbase.cluster.Cluster` returned by
    `connect_to_couchbase_cluster`, with its own API (`execute_query`, no
    `.bucket()`/`.query()`/`.collection()`). Do not pass it to helpers
    written for the operational SDK, e.g. `connect_to_bucket`.

    EA support is self-managed-only — `mcp_server.py`'s `connection_mode`
    startup guard rejects Capella connection strings before this function is
    ever called. Because of that, we explicitly disable
    `SecurityOptions.trust_only_capella` here rather than leave it at the
    SDK's own default (`True`): a self-managed cluster's certificate would
    otherwise always fail verification, since the default trusts only
    Capella's CA.

    If the connection fails, it will raise an exception.
    """
    try:
        logger.info("Connecting to Enterprise Analytics cluster...")
        credential = AnalyticsCredential.from_username_and_password(username, password)
        security_options = AnalyticsSecurityOptions(
            trust_only_capella=False,
            **({"trust_only_pem_file": ca_cert_path} if ca_cert_path else {}),
        )
        options = AnalyticsClusterOptions(security_options=security_options)

        cluster = AnalyticsCluster.create_instance(
            connection_string, credential, options
        )

        logger.info("Successfully connected to Enterprise Analytics cluster")
        return cluster
    except Exception as e:
        logger.error(
            f"Failed to connect to Enterprise Analytics cluster: {e}", exc_info=True
        )
        raise


def connect_to_bucket(cluster: Cluster, bucket_name: str) -> Bucket:
    """Connect to a bucket and return the bucket object if successful.
    If the operation fails, it will raise an exception.
    """
    try:
        logger.debug(f"Opening bucket '{bucket_name}'")
        bucket = cluster.bucket(bucket_name)
        logger.info(f"Successfully connected to bucket: {bucket_name}")
        return bucket
    except Exception as e:
        logger.error(f"Failed to connect to bucket '{bucket_name}': {e}", exc_info=True)
        raise


def format_keyspace(bucket_name: str, scope_name: str, collection_name: str) -> str:
    """Render a ``bucket.scope.collection`` keyspace string for log context."""
    return f"{bucket_name}.{scope_name}.{collection_name}"
