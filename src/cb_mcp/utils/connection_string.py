"""Helpers for parsing Couchbase connection strings."""

from urllib.parse import urlparse


def extract_hosts_from_connection_string(connection_string: str) -> list[str]:
    """Extract hosts from a connection string, e.g. 'couchbase://host1,host2'.

    Each comma-separated host:port entry is parsed with urlparse (rather than a naive
    str.split(":")) so IPv6 literals (e.g. '[::1]:8091') and userinfo (e.g. 'user:pass@host')
    are handled correctly. Empty/unparseable entries are dropped, so a malformed or empty
    connection string yields [] rather than a bogus placeholder host.
    """
    hosts = []
    for host_port in urlparse(connection_string).netloc.split(","):
        hostname = urlparse(f"//{host_port}").hostname
        if hostname:
            hosts.append(hostname)
    return hosts


def is_capella_connection(connection_string: str) -> bool:
    """Whether every host in *connection_string* is a Capella host."""
    hosts = extract_hosts_from_connection_string(connection_string)
    return bool(hosts) and all(
        host.lower().endswith(".cloud.couchbase.com") for host in hosts
    )
