import logging
import os
from datetime import timedelta

from couchbase.auth import CertificateAuthenticator, PasswordAuthenticator
from couchbase.bucket import Bucket
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.connection")


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


def parse_major_version(version_str: str | None) -> int:
    """Extract the integer major version from a Couchbase version string.

    Examples:
        - "8.0.0-1928-enterprise" -> 8
        - "7.6.0"                 -> 7

    Args:
        version_str: Node ``version`` string returned by the cluster, such as a value from ``cluster_info().nodes``.

    Returns:
        Major version as int.

    Raises:
        ValueError: If *version_str* is empty, None, or cannot be parsed.
    """
    if not version_str:
        raise ValueError("version_str is empty or None")
    major_version = version_str.strip().split(".", 1)[0]
    # Handle prefixes like "v8" defensively.
    major_version = major_version.lstrip("vV")
    try:
        return int(major_version)
    except ValueError:
        raise ValueError(f"Cannot parse major version from {version_str!r}") from None


def resolve_cluster_major_version(cluster: Cluster) -> int:
    """Detect the cluster's major version via the SDK.

    Reads the per-node ``version`` field from ``cluster.cluster_info().nodes``
    (Python SDK 4.1+) and returns the *minimum* major version across all nodes
    so we only enable the 8.x+ query-service path when every node supports it.

    The high-level helper properties (``server_version`` /
    ``server_version_short`` / ``server_version_full``) are intentionally not
    used: the SDK collapses them to ``None`` whenever the cluster reports
    mixed node versions, which is exactly the case where we still need an
    answer. Each node entry, in contrast, always carries a ``version`` string.

    Args:
        cluster: An already-connected Couchbase ``Cluster`` instance.

    Raises if cluster_info() fails — callers should not silently degrade
    when version detection is unavailable.
    """
    info = cluster.cluster_info()

    nodes = info.nodes or []
    versions: list[str] = []
    for node in nodes:
        if isinstance(node, dict):
            version = node.get("version")
        else:
            version = getattr(node, "version", None)
        if version:
            versions.append(str(version))

    if not versions:
        raise RuntimeError(
            "cluster_info() reported no nodes — cannot determine cluster version"
        )

    majors = [parse_major_version(v) for v in versions]
    min_major = min(majors)

    logger.info(f"Detected cluster node versions={versions} (min major={min_major})")
    return min_major


def format_keyspace(bucket_name: str, scope_name: str, collection_name: str) -> str:
    """Render a ``bucket.scope.collection`` keyspace string for log context."""
    return f"{bucket_name}.{scope_name}.{collection_name}"
