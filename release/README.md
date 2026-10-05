# Release versions

The backend version is defined in `release/release-identity.json`. The independent Admin version is defined in `admin/package.json`. Use Python 3.11+ from the repository root:

```sh
python scripts/release_version.py check
python scripts/release_version.py set --backend 0.4.6 --admin 0.5.77
python scripts/release_version.py check
```

Either version argument can be omitted to retain that component's version. For a separate Admin checkout, pass `--admin-dir /path/to/admin`. Before setting a release, prepare `admin/release-notes/release-notes-v<admin-version>.md` with a version heading, a change list, and two metadata lines ending in a backtick-quoted numeric version: Admin first, backend second. Labels may be in Russian or English. The command only verifies this file; it never edits, translates, or copies release notes. Keep version mentions in the prose accurate when writing those notes.

The command updates and checks Familia metadata, runtime version, Dockerfile, Compose defaults, release identity, Admin package/Tauri/Cargo metadata (including only the Admin entry in Cargo.lock), and current-version documentation. It checks the current release notes separately and preserves all release notes, dependency versions, and test examples. All mandatory bindings are checked before the first write; multi-file writes are not atomic. Run `check` after an interruption and inspect both Git diffs before committing.

The existing `scripts/check_release_identity.py --backend-version <version> --tag image-v<version>` remains the backend-only packaging/dependency check. It no longer hardcodes a release number. `release_version.py check` includes that check and requires the Admin checkout. Neither command commits, pushes, builds, or publishes anything. Run validation in the approved environment (Docker for this task).
