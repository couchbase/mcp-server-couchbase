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

# Update server.json (root version, package versions, and Docker image tags).
# server.json is the single MCP Registry listing; every server it ships is a
# package entry inside it (see RELEASE.md).
if ! command -v jq &> /dev/null; then
  echo "Error: jq is required but not installed"
  echo "Install with: brew install jq (macOS) or apt install jq (Linux)"
  exit 1
fi

echo "Updating server.json..."
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
' server.json > server.json.tmp
mv server.json.tmp server.json

# Update lock file
echo "Updating uv.lock"
uv lock

echo ""
echo "Version updated to $NEW_VERSION in:"
echo "   - pyproject.toml"
echo "   - server.json (root, packages, and Docker image tags)"
echo "   - uv.lock"
echo ""
echo "Verification:"
echo "   pyproject.toml: $(grep '^version = ' pyproject.toml)"
echo "   server.json root: $(jq -r '.version' server.json)"
echo "   server.json packages:"
jq -r '.packages[] |
  if .registryType == "oci" then
    "     - \(.registryType):\(.identifier) \(.packageArguments[0].value // "") (tag: \(.identifier | split(":")[1]))"
  else
    "     - \(.registryType):\(.identifier) \(.packageArguments[0].value // "") (version: \(.version))"
  end' server.json
echo ""
echo "Next steps:"
echo "   1. Review changes: git diff"
echo "   2. Commit: git add pyproject.toml server.json uv.lock && git commit -m 'Bump version to $NEW_VERSION'"
echo "   3. Tag: git tag v$NEW_VERSION"
echo "   4. Push: git push origin main && git push origin v$NEW_VERSION"
