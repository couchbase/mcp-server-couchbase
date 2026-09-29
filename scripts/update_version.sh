#!/bin/bash
set -e

# Simple script to update version in all necessary files
# Usage: ./scripts/update_version.sh 0.5.2

if [ -z "$1" ]; then
  echo "Usage: $0 <new_version>"
  echo "Example: $0 0.5.2"
  echo "Example: $0 0.5.2rc1"
  exit 1
fi

NEW_VERSION="$1"

echo "Updating version to $NEW_VERSION"
echo ""

# Update pyproject.toml
echo "Updating pyproject.toml..."
if [[ "$OSTYPE" == "darwin"* ]]; then
  # macOS
  sed -i '' "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
else
  # Linux
  sed -i "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
fi

# Update server.json and operational_insights_server.json (root version,
# package versions, and Docker image tags). Two files: one MCP Registry
# listing per server (see RELEASE.md).
if ! command -v jq &> /dev/null; then
  echo "Error: jq is required but not installed"
  echo "Install with: brew install jq (macOS) or apt install jq (Linux)"
  exit 1
fi

for manifest in server.json operational_insights_server.json; do
  echo "Updating $manifest..."
  jq --arg ver "$NEW_VERSION" '
    .version = $ver |
    .packages = [
      .packages[] |
      if .registryType == "oci" then
        # Update Docker image tag (everything after last :)
        # OCI packages should NOT have a separate version field
        .identifier = (.identifier | sub(":[^:]*$"; ":" + $ver))
      else
        # Update version field for non-OCI packages (e.g., PyPI)
        .version = $ver
      end
    ]
  ' "$manifest" > "$manifest.tmp"
  mv "$manifest.tmp" "$manifest"
done

# Update lock file
echo "Updating uv.lock"
uv lock

echo ""
echo "Version updated to $NEW_VERSION in:"
echo "   - pyproject.toml"
echo "   - server.json (root, packages, and Docker image tags)"
echo "   - operational_insights_server.json (root, packages, and Docker image tags)"
echo "   - uv.lock"
echo ""
echo "Verification:"
echo "   pyproject.toml: $(grep '^version = ' pyproject.toml)"
for manifest in server.json operational_insights_server.json; do
  echo "   $manifest root: $(jq -r '.version' "$manifest")"
  echo "   $manifest packages:"
  jq -r '.packages[] |
    if .registryType == "oci" then
      "     - \(.registryType):\(.identifier) (tag: \(.identifier | split(":")[1]))"
    else
      "     - \(.registryType):\(.identifier) (version: \(.version))"
    end' "$manifest"
done
echo ""
echo "Next steps:"
echo "   1. Review changes: git diff"
echo "   2. Commit: git add pyproject.toml server.json operational_insights_server.json uv.lock && git commit -m 'Bump version to $NEW_VERSION'"
echo "   3. Tag: git tag v$NEW_VERSION"
echo "   4. Push: git push origin main && git push origin v$NEW_VERSION"
