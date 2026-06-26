#!/usr/bin/env python3
"""
Setup script for integration tests.

This script:
1. Creates required indexes for travel-sample bucket
2. Waits for indexes to be built
3. Enables query logging in Couchbase (sets completed-threshold to 0)
4. Runs various queries to populate system:completed_requests
5. Ensures performance analysis tests have data to validate against

Usage:
    uv run scripts/setup_test_data.py

Environment variables required:
    CB_CONNECTION_STRING - Couchbase connection string (e.g., couchbase://localhost)
    CB_USERNAME - Couchbase username
    CB_PASSWORD - Couchbase password
    CB_MCP_TEST_BUCKET - Test bucket name (e.g., travel-sample)

This script should be run before pytest to ensure tests don't skip.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import timedelta

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions


def get_env_or_exit(var_name: str) -> str:
    """Get environment variable or exit with error."""
    value = os.getenv(var_name)
    if not value:
        print(f"Error: {var_name} environment variable is required")
        sys.exit(1)
    return value


def enable_query_logging(connection_string: str, username: str, password: str) -> bool:
    """Enable query logging by setting completed-threshold to 0."""
    # Extract host from connection string
    host = connection_string.replace("couchbase://", "").replace("couchbases://", "")
    host = host.split(",")[0]  # Take first host if multiple
    host = host.split(":")[0]  # Remove port if present

    url = f"http://{host}:8093/admin/settings"
    data = json.dumps({"completed-threshold": 0, "completed-limit": 10000}).encode()

    # Create request with basic auth
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {credentials}",
    }

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                print("  - Query logging enabled (completed-threshold=0)")
                return True
    except urllib.error.URLError as e:
        print(f"  - Warning: Could not enable query logging: {e}")
        print("    (Performance tests may be skipped)")
    except Exception as e:
        print(f"  - Warning: Error enabling query logging: {e}")

    return False


def create_indexes(cluster: Cluster, bucket_name: str) -> None:
    """Create required indexes for tests."""
    print("\n2. Creating indexes...")

    # Define indexes to create
    indexes = [
        # Primary index on _default._default for tests that use default scope
        (
            f"CREATE PRIMARY INDEX IF NOT EXISTS ON `{bucket_name}`.`_default`.`_default`",
            "primary index on _default._default",
        ),
        # Legacy bucket-level index: created with the pre-scopes, unqualified
        # `ON `bucket`(...)` DDL (no scope/collection), so it lands in
        # system:indexes with keyspace_id=<bucket> and no bucket_id/scope_id.
        # This is what
        # test_list_indexes_filtered_by_bucket_includes_legacy_indexes verifies;
        # without a legacy index present that regression test just skips.
        (
            f"CREATE INDEX IF NOT EXISTS idx_legacy_default ON `{bucket_name}`(`type`)",
            "idx_legacy_default (legacy bucket-level index)",
        ),
        # Primary index on inventory.airline for index tests
        (
            f"CREATE PRIMARY INDEX IF NOT EXISTS ON `{bucket_name}`.`inventory`.`airline`",
            "primary index on inventory.airline",
        ),
        # Secondary index on inventory.airline for better test coverage
        (
            f"CREATE INDEX IF NOT EXISTS idx_airline_country ON `{bucket_name}`.`inventory`.`airline`(country)",
            "idx_airline_country on airline",
        ),
        # Index on route.sourceairport for non-selective queries
        (
            f"CREATE INDEX IF NOT EXISTS idx_route_sourceairport ON `{bucket_name}`.`inventory`.`route`(sourceairport)",
            "idx_route_sourceairport on route",
        ),
        # Index on airport.city for non-selective queries
        (
            f"CREATE INDEX IF NOT EXISTS idx_airport_city ON `{bucket_name}`.`inventory`.`airport`(city)",
            "idx_airport_city on airport",
        ),
        # Index on airport.faa for non-selective queries
        (
            f"CREATE INDEX IF NOT EXISTS idx_airport_faa ON `{bucket_name}`.`inventory`.`airport`(faa)",
            "idx_airport_faa on airport",
        ),
        # Index on hotel.city for non-selective queries
        (
            f"CREATE INDEX IF NOT EXISTS idx_hotel_city ON `{bucket_name}`.`inventory`.`hotel`(city)",
            "idx_hotel_city on hotel",
        ),
    ]

    for statement, description in indexes:
        try:
            cluster.query(statement).execute()
            print(f"  - Created {description}")
        except Exception as e:
            # Index may already exist or scope/collection may not exist
            error_msg = str(e)
            if "already exists" in error_msg.lower():
                print(f"  - {description} already exists")
            elif (
                "not found" in error_msg.lower()
                or "does not exist" in error_msg.lower()
            ):
                print(f"  - Skipped {description} (scope/collection not found)")
            else:
                print(f"  - Warning: Could not create {description}: {e}")


def wait_for_indexes(cluster: Cluster, timeout_seconds: int = 180) -> bool:
    """Wait for all indexes to be built (online state)."""
    print("\n3. Waiting for indexes to be built...")

    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            result = cluster.query(
                "SELECT COUNT(*) as cnt FROM system:indexes WHERE state != 'online'"
            )
            rows = list(result)
            building_count = rows[0].get("cnt", 0) if rows else 0

            if building_count == 0:
                print("  - All indexes are online")
                return True

            elapsed = int(time.time() - start_time)
            print(
                f"  - Waiting for {building_count} indexes to be built... ({elapsed}s elapsed)"
            )
            time.sleep(3)

        except Exception as e:
            print(f"  - Warning: Could not check index status: {e}")
            time.sleep(3)

    print(f"  - Warning: Timed out waiting for indexes after {timeout_seconds}s")
    return False


def list_indexes(cluster: Cluster, bucket_name: str) -> None:
    """List all indexes for debugging."""
    print("\n  Index status:")
    try:
        result = cluster.query(
            "SELECT name, bucket_id, scope_id, keyspace_id, state "
            "FROM system:indexes "
            f"WHERE bucket_id = '{bucket_name}' OR bucket_id IS MISSING"
        )
        for row in result:
            name = row.get("name", "unknown")
            scope = row.get("scope_id", "_default")
            keyspace = row.get("keyspace_id", "unknown")
            state = row.get("state", "unknown")
            print(f"    - {name} on {scope}.{keyspace}: {state}")
    except Exception as e:
        print(f"    Could not list indexes: {e}")


def check_scope_exists(bucket, scope_name: str) -> bool:
    """Check if a scope exists in the bucket."""
    try:
        scopes = bucket.collections().get_all_scopes()
        return any(s.name == scope_name for s in scopes)
    except Exception:
        return False


def _run_inventory_select_queries(scope) -> None:
    """Run regular SELECT and PRIMARY index queries."""
    print("  - Running regular SELECT queries...")
    for _ in range(3):
        result = scope.query("SELECT * FROM airline LIMIT 100")
        list(result)

    result = scope.query("SELECT * FROM route LIMIT 500")
    list(result)

    print("  - Running queries using primary index...")
    try:
        result = scope.query(
            "SELECT META().id, * FROM airline WHERE LOWER(name) LIKE '%air%' LIMIT 10"
        )
        list(result)
    except Exception:
        pass  # May fail depending on data


def _run_inventory_fetch_queries(scope) -> None:
    """Run queries requiring fetch after index scan (not using covering index)."""
    print("  - Running queries requiring fetch after index scan...")
    result = scope.query(
        "SELECT * FROM airline WHERE country = 'United States' LIMIT 50"
    )
    list(result)

    for _ in range(3):
        result = scope.query(
            "SELECT id, name, type, iata FROM airline WHERE country = 'United States'"
        )
        list(result)


def _run_inventory_non_selective_queries(scope) -> None:
    """Run non-selective queries (indexScan > resultCount)."""
    print("  - Running non-selective queries (using secondary indexes)...")

    non_selective_queries = [
        """
            SELECT * FROM route
            WHERE sourceairport >= 'A' AND sourceairport < 'Z'
            AND destinationairport = 'XXXXX'
        """,
        """
            SELECT * FROM airport
            WHERE city >= 'A' AND city < 'Z'
            AND faa = 'XXXX'
        """,
        """
            SELECT * FROM hotel
            WHERE city >= 'A' AND city < 'Z'
            AND name LIKE 'ZZZZZZ%'
        """,
        """
            SELECT * FROM airport
            WHERE faa >= 'A' AND faa < 'Z'
            AND country = 'XXXXXX'
        """,
    ]
    for q in non_selective_queries:
        try:
            result = scope.query(q)
            list(result)
        except Exception:
            pass


def _run_inventory_varied_queries(scope) -> None:
    """Run additional varied queries."""
    print("  - Running additional varied queries...")
    varied_queries = [
        "SELECT COUNT(*) as cnt FROM airline",
        "SELECT DISTINCT country FROM airline",
        "SELECT name, country FROM airline ORDER BY name LIMIT 20",
        "SELECT name, keyspace_id, index_key FROM system:indexes WHERE bucket_id = 'travel-sample' AND state = 'online'",
        "SELECT * FROM airport WHERE faa >= 'A' AND faa < 'Z' AND country = 'XXXXXX'",
        "SELECT * FROM airport WHERE faa >= 'A' AND faa < 'Z' AND country = 'XXXXXX'",
        "SELECT * FROM hotel WHERE city >= 'A' AND city < 'Z' AND name LIKE 'ZZZZZZ%'",
    ]
    for q in varied_queries:
        result = scope.query(q)
        list(result)


def run_test_queries_inventory(cluster: Cluster, bucket_name: str) -> None:
    """Run queries against travel-sample inventory scope (full sample data)."""
    bucket = cluster.bucket(bucket_name)
    scope = bucket.scope("inventory")

    print("  Using inventory scope (travel-sample with sample data)...")

    _run_inventory_select_queries(scope)
    _run_inventory_fetch_queries(scope)
    _run_inventory_non_selective_queries(scope)
    _run_inventory_varied_queries(scope)


def run_test_queries_default(cluster: Cluster, bucket_name: str) -> None:
    """Run queries against _default scope (CI environment with basic bucket)."""
    bucket = cluster.bucket(bucket_name)
    scope = bucket.scope("_default")
    collection_name = "_default"

    print("  Using _default scope (CI environment)...")

    # Regular SELECT queries (for longest running, most frequent, response sizes)
    print("  - Running regular SELECT queries...")
    for _ in range(5):
        try:
            result = scope.query(f"SELECT * FROM `{collection_name}` LIMIT 100")
            list(result)
        except Exception:
            pass

    # Queries using PRIMARY index
    print("  - Running queries using primary index...")
    try:
        result = scope.query(f"SELECT META().id, * FROM `{collection_name}` LIMIT 10")
        list(result)
    except Exception:
        pass

    # Queries with filters
    print("  - Running queries with filters...")
    try:
        result = scope.query(
            f"SELECT * FROM `{collection_name}` WHERE type = 'test' LIMIT 50"
        )
        list(result)
    except Exception:
        pass

    for _ in range(3):
        try:
            result = scope.query(f"SELECT * FROM `{collection_name}` WHERE id > 0")
            list(result)
        except Exception:
            pass

    # Additional varied queries
    print("  - Running additional varied queries...")
    try:
        result = scope.query(f"SELECT COUNT(*) as cnt FROM `{collection_name}`")
        list(result)
    except Exception:
        pass

    try:
        result = scope.query(f"SELECT DISTINCT type FROM `{collection_name}`")
        list(result)
    except Exception:
        pass

    try:
        result = scope.query(
            f"SELECT * FROM `{collection_name}` ORDER BY META().id LIMIT 20"
        )
        list(result)
    except Exception:
        pass


def run_test_queries(cluster: Cluster, bucket_name: str) -> None:
    """Run various queries to populate system:completed_requests."""
    bucket = cluster.bucket(bucket_name)

    print("\n4. Running queries to populate system:completed_requests...")

    # Check if inventory scope exists (travel-sample with full data)
    if check_scope_exists(bucket, "inventory"):
        run_test_queries_inventory(cluster, bucket_name)
    else:
        # Fall back to _default scope (CI environment)
        run_test_queries_default(cluster, bucket_name)


def verify_completed_requests(cluster: Cluster) -> int:
    """Verify that system:completed_requests has data."""
    result = cluster.query("""
        SELECT COUNT(*) as cnt
        FROM system:completed_requests
        WHERE UPPER(statement) NOT LIKE 'INFER %'
            AND UPPER(statement) NOT LIKE '% SYSTEM:%'
    """)
    rows = list(result)
    return rows[0].get("cnt", 0) if rows else 0


def main() -> int:
    """Main entry point."""
    print("Setting up test data for integration tests...")

    # Get required environment variables
    connection_string = get_env_or_exit("CB_CONNECTION_STRING")
    username = get_env_or_exit("CB_USERNAME")
    password = get_env_or_exit("CB_PASSWORD")
    bucket_name = get_env_or_exit("CB_MCP_TEST_BUCKET")

    print(f"\nConnecting to {connection_string}...")
    print(f"Using bucket: {bucket_name}")

    # Enable query logging first (before connecting with SDK)
    print("\n1. Enabling query logging...")
    enable_query_logging(connection_string, username, password)

    # Connect to cluster
    auth = PasswordAuthenticator(username, password)
    cluster = Cluster(connection_string, ClusterOptions(auth))

    try:
        cluster.wait_until_ready(timedelta(seconds=30))
        print("  - Connected to cluster")

        # Create indexes
        create_indexes(cluster, bucket_name)

        # Wait for indexes to be built
        wait_for_indexes(cluster)

        # List indexes for debugging
        list_indexes(cluster, bucket_name)

        # Run test queries
        run_test_queries(cluster, bucket_name)

        # Verify data
        print("\n5. Verifying system:completed_requests...")
        count = verify_completed_requests(cluster)
        print(f"  - Found {count} completed requests")

        if count > 0:
            print("\n✓ Test data setup complete!")
            return 0
        else:
            print("\n⚠ Warning: No completed requests found.")
            print("  Performance analysis tests may be skipped.")
            return 0  # Don't fail, tests will skip gracefully

    except Exception as e:
        print(f"\nError: {e}")
        return 1
    finally:
        cluster.close()


if __name__ == "__main__":
    sys.exit(main())
