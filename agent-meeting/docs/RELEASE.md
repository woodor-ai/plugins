# agent-meeting release standard

This document is the authority for packaging and publishing `agent-meeting`.
Repository-wide release requirements are defined in
[`../../docs/RELEASE.md`](../../docs/RELEASE.md) and apply in addition to this
plugin-specific standard.

`agent-meeting` is not released from a marketplace checkout. Its complete
installer, updater, and first-use runtime bootstrap use Cloudflare R2 through
`dl.omi-atlas.com`. GitHub remains the source repository, but it is not a
runtime download origin.

## Version authorities

An agent-meeting release uses one semantic version in all of these locations:

- Claude Code and Codex plugin manifests;
- `agent-meeting/pyproject.toml` and the package `__version__`;
- `mycodex/pyproject.toml`, its pinned agent-meeting dependency, and package
  `__version__`;
- the public installer `RELEASE` value;
- version assertions in distribution, integration, activation, and uninstall
  tests;
- `agent-meeting/docs/CLI_SURFACE.md`.

The release commit receives the repository tag `vX.Y.Z`. Do not publish a
cachebuster suffix or retain acceptance of an older version form.

## R2 layout and public URLs

Bucket: `omi-dist`

| R2 object key | Public URL | Cache policy | Purpose |
| --- | --- | --- | --- |
| `am` | `https://dl.omi-atlas.com/am` | `no-store, max-age=0` | short one-command installer |
| `am/install.py` | `https://dl.omi-atlas.com/am/install.py` | `no-store, max-age=0` | stable installer fetched by `am-update` |
| `am/releases/vX.Y.Z/agent-meeting.zip` | `https://dl.omi-atlas.com/am/releases/vX.Y.Z/agent-meeting.zip` | `public, max-age=31536000, immutable` | immutable minimal agent-meeting source bundle |

The two stable installer objects contain identical bytes. They select one
immutable release bundle; publish the bundle first, then replace the stable
installer objects. Never overwrite or delete a versioned bundle. The bundle
name and its top-level directory must use `agent-meeting`, never the repository
name `plugins`.

## Installation and update contract

macOS and Linux:

```sh
curl -fsSL https://dl.omi-atlas.com/am | python3 -
```

Windows PowerShell:

```powershell
irm https://dl.omi-atlas.com/am | py -3 -
```

Existing installations update with:

```text
am-update
am-update --target claude-code
am-update --target codex
am-update --check
```

`am-update` downloads `https://dl.omi-atlas.com/am/install.py`; that installer
downloads the versioned R2 bundle. The bundled first-use bootstrap derives the
same R2 bundle URL from its plugin manifest version. Neither path may clone a
repository, read a local checkout, or download a GitHub archive. Legacy local
checkouts may only be removed as migration cleanup.

## Packaging and publishing

For release `vX.Y.Z`:

1. Bump every version authority listed above and update current docs.
2. Run the full test suite and platform-specific installer tests.
3. Commit and push `main`, create annotated tag `vX.Y.Z` on that commit, and
   push the tag over SSH.
4. Build the immutable bundle with the repository's dedicated builder. The
   builder has the only authoritative file allowlist and rejects unrelated
   top-level content:

   ```sh
   python3 installers/build-agent-meeting-release.py \
     --ref "vX.Y.Z" \
     --output "/tmp/agent-meeting-vX.Y.Z.zip"
   ```

5. Confirm the bundle contains exactly these agent-meeting installation
   inputs under `agent-meeting-vX.Y.Z/`:

   - `agent-meeting` README, package source, manifests, skills, and runtime
     bootstrap;
   - `mycodex` package source;
   - the unified installer and shared installer stages;
   - `LICENSE`.

   The bundle must not contain other plugins, repository-level docs or rules,
   tests, marketplace catalogs, or project-level names such as `plugins`.
6. Upload the immutable bundle before either stable installer object:

   ```sh
   wrangler r2 object put \
     "omi-dist/am/releases/vX.Y.Z/agent-meeting.zip" \
     --file "/tmp/agent-meeting-vX.Y.Z.zip" \
     --content-type "application/zip" \
     --cache-control "public, max-age=31536000, immutable" \
     --remote

   wrangler r2 object put "omi-dist/am" \
     --file "installers/public/agent-meeting-install.py" \
     --content-type "text/x-python; charset=utf-8" \
     --cache-control "no-store, max-age=0" \
     --remote

   wrangler r2 object put "omi-dist/am/install.py" \
     --file "installers/public/agent-meeting-install.py" \
     --content-type "text/x-python; charset=utf-8" \
     --cache-control "no-store, max-age=0" \
     --remote
   ```

   Use the existing Wrangler login or the R2-scoped credentials referenced by
   the local secrets memo. Never copy credentials into this repository.
7. Download all three public objects. Compare SHA-256 for the bundle and both
   installer objects against the local files, inspect response cache headers,
   and run ZIP integrity validation on the delivered bundle.
8. Run one clean-install smoke test and one `am-update` smoke test. Validate
   macOS locally and Windows on the designated LAN machine. A running
   `am-codexd` with active sessions must defer its version switch rather than
   interrupt those sessions.

## Rollback

Keep every versioned R2 bundle immutable. To roll back, restore both stable
installer objects from the prior release tag, upload those identical bytes
with the stable no-store cache policy, and verify delivery again. Do not move a
tag or overwrite a released bundle.
