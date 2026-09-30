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

> **Note:** `server.json` is the single MCP Registry listing
> (`io.github.couchbase/mcp-server-couchbase`) for every server this
> distribution ships. Each server gets its own package entries (one PyPI,
> one OCI). They all point at the same `couchbase-mcp-server` PyPI package
> and `docker.io/couchbase/mcp-server` image, and each one selects its
> server with a positional subcommand (`operational` or
> `operational-insights`). All entries must stay in sync with the release
> version.

**Option B: Manual update:**

Update the version in all locations. Steps 3-4 apply to **every** package
entry in `server.json` (one PyPI and one OCI entry per server).

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

3. **`server.json`** (each PyPI package version):

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

4. **`server.json`** (Docker image tag in each OCI package):

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

> **Important:** All package versions and Docker image tags must match the root version. The CI/CD pipeline validates this and will fail if versions are inconsistent.

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
    "  - \(.registryType):\(.identifier) \(.packageArguments[0].value // "") (tag: \(.identifier | split(":")[1]))"
  else
    "  - \(.registryType):\(.identifier) \(.packageArguments[0].value // "") (version: \(.version))"
  end' server.json
```

**Expected output:**

- All versions should be `0.5.2`
- Every Docker image tag in `server.json` should be `:0.5.2`

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

Once the tag is pushed, the following GitHub Actions workflows run sequentially:

1. **Release and Build Python Wheels**
   - Creates GitHub Release with auto-generated changelog
   - Triggers a Jenkins job to build the distribution wheel

2. **Test PyPI Upload** and **PyPI Upload** (both triggered in parallel after step 1)
   - Test PyPI Upload publishes to TestPyPI via Jenkins for QE validation
   - PyPI Upload publishes the wheel to production PyPI as `couchbase-mcp-server` via Jenkins

3. **Docker Build** (triggered after PyPI Upload completes)
   - Triggers a Jenkins job to publish `couchbase/mcp-server` to Docker Hub
   - Stable releases (`vX.Y.Z`) are tagged with both the version and `latest`
   - Pre-releases are tagged with the version only

4. **MCP Registry Update** (runs after Docker completes)
   - Waits for both PyPI and Docker to complete
   - Validates version consistency for `server.json`
   - Publishes `server.json` to the MCP Registry as a single listing that
     covers every server

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
- MCP Registry: version `0.5.2rc1` of the single listing
  `io.github.couchbase/mcp-server-couchbase` (from `server.json`)

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
- **`server.json` root `version`**: MCP Registry metadata version for the listing
- **`server.json` package `version`**: Every PyPI package entry must match root version
- **`server.json` OCI identifiers**: Every Docker image tag must match root version
- **Git tag**: Must match all versions

### Why All Versions Must Match

The CI/CD pipeline validates version consistency in `server.json` and will **fail the build** if:

- Package versions don't match the root version
- A Docker image tag in an OCI identifier doesn't match the root version
- Root version doesn't match the git tag
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

This updates `pyproject.toml` and `server.json`, and runs `uv lock`, in one command.
