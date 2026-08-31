"""Helpers for parsing Couchbase connection strings."""

from urllib.parse import urlparse


def extract_hosts_from_connection_string(connection_string: str) -> list[str]:
    """Extract hosts from a connection string, e.g. 'couchbase://host1,host2'."""
    return [
        host.split(":")[0] for host in urlparse(connection_string).netloc.split(",")
    ]


def is_capella_connection(connection_string: str) -> bool:
    """Whether every host in *connection_string* is a Capella host."""
    hosts = extract_hosts_from_connection_string(connection_string)
    return bool(hosts) and all(
        host.lower().endswith(".cloud.couchbase.com") for host in hosts
    )
