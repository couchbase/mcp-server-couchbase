# Release Process

This document describes how to create a new release of `mcp-server-couchbase`.

## How to Release a New Version

### Update Version Numbers

**Option A: Use the helper script (Recommended):**

```bash
./scripts/update_version.sh 0.5.2
```

This automatically updates:

- `pyproject.toml` version
- `server.json` root version
- `server.json` all package versions
- `server.json` Docker image tags (OCI identifiers)
- `uv.lock`

**Option B: Manual update:**

Update the version in all locations:

1. **`pyproject.toml`:**

   ```toml
   version = "0.5.2"
   ```

2. **`server.json`** (root version):

   ```json
   {
     "version": "0.5.2",
     ...
   }
   ```

3. **`server.json`** (each package version):

   ```json
   {
     "packages": [
       {
         "version": "0.5.2",
         ...
       }
     ]
   }
   ```

4. **`server.json`** (Docker image tags in OCI packages):

   ```json
   {
     "packages": [
       {
         "registryType": "oci",
         "identifier": "docker.io/couchbase/mcp-server:0.5.2",
         ...
       }
     ]
   }
   ```

5. Update lock file:

   ```bash
   uv lock
   ```

> **Important:** All versions and Docker image tags must match the root version. The CI/CD pipeline validates this and will fail if versions are inconsistent.

### 2. Validate Versions

Before pushing, verify all versions match:

```bash
# Check versions
echo "Checking version consistency..."
echo "pyproject.toml: $(grep '^version = ' pyproject.toml)"
echo "server.json root: $(jq -r '.version' server.json)"
echo "server.json packages:"
jq -r '.packages[] |
  if .registryType == "oci" then
    "  - \(.registryType):\(.identifier) (tag: \(.identifier | split(":")[1]))"
  else
    "  - \(.registryType):\(.identifier) (version: \(.version))"
  end' server.json
```

**Expected output:**

- All versions should be `0.5.2`
- Docker image tag should be `:0.5.2`

If versions don't match, run `./scripts/update_version.sh 0.5.2` again.

### 3. Open a Version-Bump PR

Land the version changes on `main` through a pull request rather than pushing
directly. The PR build runs the Docker workflow with `push: false`, giving you
a final sanity check that the image builds before any release artefacts are
published.

```bash
git checkout -b bump-version-0.5.2
git add pyproject.toml server.json uv.lock
git commit -m "Bump version to 0.5.2"
git push origin bump-version-0.5.2
```

Open the PR, get it reviewed, and merge it into `main` using whichever merge
strategy the project normally uses (squash / rebase / merge commit are all
fine — the tag in step 4 will point at whatever main resolves to).

### 4. Tag the Merged Commit on `main`

**Do not tag the PR head.** Squash and rebase merges produce new SHAs on
`main`, so a tag on the PR head would point to a commit that is not reachable
from `main`'s tip — the release workflows would then build the wrong tree.
Always tag the commit that actually landed on `main`:

```bash
git checkout main
git pull origin main
git tag v0.5.2
git push origin v0.5.2
```

> **Important:** Once you push the tag, **all workflows start immediately**
> and PyPI publishes within ~3 minutes. PyPI versions are **immutable** — if
> anything fails later, you'll need a new version number. There's no going
> back!

### 5. Automated Pipeline

Once the tag is pushed, three GitHub Actions workflows run **in parallel/sequence**:

1. **PyPI Release**
   - Builds Python package
   - Publishes to PyPI as `couchbase-mcp-server`
   - Creates GitHub Release with changelog

2. **Docker Build**
   - Builds multi-architecture images (amd64, arm64)
   - Pushes to Docker Hub as `couchbase/mcp-server`
   - Updates Docker Hub description

3. **MCP Registry Update** (runs after Docker completes)
   - Waits for both PyPI and Docker to complete
   - Validates version consistency
   - Publishes to MCP Registry

> **Note:** Version validation happens in the MCP Registry workflow, which runs **after** PyPI and Docker have already published. This is why local validation (step 2) is critical!

> **Note:** The MCP registry entry can be validated using this [third party option](https://registry.teamspark.ai/tester) before releasing or for debugging.

### 6. Verify Release

Check that all three workflows succeeded:

- [GitHub Actions](https://github.com/couchbase/mcp-server-couchbase/actions)

Verify the release is available on:

- [PyPI](https://pypi.org/project/couchbase-mcp-server/)
- [Docker Hub](https://hub.docker.com/r/couchbase/mcp-server)
- [MCP Registry](https://hub.docker.com/mcp/server/couchbase/overview)

There is a delay between PyPI/Docker publish and MCP Registry update due to the images being built independently by Docker on a regular schedule. So check the MCP Registry the next day.

## Release Candidates

**Recommended for first-time releases or major changes.**

Release candidates let you test the full release pipeline without committing to a final version number. If something fails, you can fix it and release the final version without version conflicts.

**Create an RC release:**

```bash
# Update version to RC
./scripts/update_version.sh 0.5.2rc1

# Or manually update pyproject.toml and server.json (root + all packages)

# Open a PR with the RC bump
git checkout -b bump-version-0.5.2rc1
git add pyproject.toml server.json uv.lock
git commit -m "Bump version to 0.5.2rc1"
git push origin bump-version-0.5.2rc1

# After the PR merges, tag the resulting commit on main
git checkout main
git pull origin main
git tag v0.5.2rc1
git push origin v0.5.2rc1
```

**What gets published:**

- PyPI: `couchbase-mcp-server==0.5.2rc1`
- Docker Hub: `couchbase/mcp-server:0.5.2rc1`
- MCP Registry: version `0.5.2rc1`

**If RC succeeds, release the final version:**

```bash
./scripts/update_version.sh 0.5.2

git checkout -b bump-version-0.5.2
git add pyproject.toml server.json uv.lock
git commit -m "Bump version to 0.5.2"
git push origin bump-version-0.5.2

# After the PR merges, tag main
git checkout main
git pull origin main
git tag v0.5.2
git push origin v0.5.2
```

**If RC fails:**

- Fix the issues
- Create `0.5.2rc2` and test again
- No version conflicts since the final `0.5.2` wasn't published yet!

## Troubleshooting

### Release Failed

**IMPORTANT:** Once PyPI publishes a version, it **cannot be reused**. PyPI versions are immutable.

If a release fails after PyPI has published (e.g., Docker build fails, MCP Registry update fails):

**Skip to next patch version:**

```bash
# If v0.5.2 was published but release incomplete
./scripts/update_version.sh 0.5.3

git checkout -b bump-version-0.5.3
git add pyproject.toml server.json uv.lock
git commit -m "Bump version to 0.5.3"
git push origin bump-version-0.5.3

# After the PR merges, tag main
git checkout main
git pull origin main
git tag v0.5.3
git push origin v0.5.3
```

**Why this happens:**

- PyPI, Docker, and MCP Registry workflows all start when you push the tag
- Version validation only happens in the MCP Registry workflow (which runs last)
- By that time, PyPI and Docker have already published
- If validation or MCP Registry publish fails, you can't reuse the version number

**Prevention:**

- **Always test with RC releases first** (e.g., `0.5.2rc1`)
- **Use the helper script** (`./scripts/update_version.sh`) to ensure all versions match
- **Review the version-bump PR carefully** before merging, and confirm `main` is at the expected commit before tagging
- Verify all workflows succeeded before announcing release

## How Versioning Works

### Version Files

All version numbers must be **manually synchronized** across:

- **`pyproject.toml`**: Python package version
- **`server.json` root `version`**: MCP Registry metadata version
- **`server.json` package `version`**: Must match root version
- **`server.json` OCI identifiers**: Docker image tags must match root version
- **Git tag**: Must match all versions

### Why All Versions Must Match

The CI/CD pipeline validates version consistency and will **fail the build** if:

- Package versions in `server.json` don't match the root version
- Docker image tags in OCI identifiers don't match the root version
- Root version in `server.json` doesn't match the git tag
- (Warning only) `pyproject.toml` doesn't match the git tag

This ensures:

- No accidental version mismatches
- Consistent versioning across PyPI, Docker, and MCP Registry
- Valid JSON files that can be tested locally
- Clear version history in git

### Helper Script

The `scripts/update_version.sh` script keeps all versions synchronized automatically:

```bash
./scripts/update_version.sh 0.5.2
```

This updates all three locations and runs `uv lock` in one command.
