from __future__ import annotations

import argparse
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import release_version


class ReleaseVersionTests(unittest.TestCase):
    def test_version_requires_a_plain_numeric_release(self) -> None:
        for value in ("v0.4.6", "0.4", "01.4.6", "0.4.6-rc1", "0.4.6\n"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                release_version.version(value)

    def test_bump_and_preflight_preserve_history_and_dependencies(self) -> None:
        identity = json.loads((ROOT / "release/release-identity.json").read_text())
        package = json.loads((ROOT / "admin/package.json").read_text())
        current = release_version.updates(ROOT, ROOT / "admin", identity["backend_version"], package["version"])
        # This test runs only inside Docker. Keep its fixture for inspection.
        fixture = Path("/tmp") / f"familia-release-version-{uuid.uuid4().hex}"
        for path in current:
            destination = fixture / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        old_notes = ROOT / f"admin/release-notes/release-notes-v{package['version']}.md"
        (fixture / "admin/release-notes").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old_notes, fixture / old_notes.relative_to(ROOT))
        notes = fixture / "admin/release-notes/release-notes-v9.8.7.md"
        notes.write_text("# v9.8.7\n\n- Release changes.\n\nAdmin: `9.8.7`.\n\nBackend: `9.8.6`.\n")
        cargo = fixture / "admin/src-tauri/Cargo.lock"
        before_cargo = cargo.read_text()
        before_notes = (fixture / f"admin/release-notes/release-notes-v{package['version']}.md").read_text()
        planned = release_version.updates(fixture, fixture / "admin", "9.8.6", "9.8.7")
        for path, text in planned.items():
            path.write_text(text)
        self.assertEqual(release_version.updates(fixture, fixture / "admin", "9.8.6", "9.8.7"), planned)
        updated_identity = json.loads((fixture / "release/release-identity.json").read_text())
        self.assertEqual(updated_identity["backend_version"], "9.8.6")
        self.assertEqual(updated_identity["release_tag"], "image-v9.8.6")
        self.assertEqual(updated_identity["component_versions"]["familia"], "9.8.6")
        self.assertEqual(
            cargo.read_text().replace('name = "familia-admin"\nversion = "9.8.7"', f'name = "familia-admin"\nversion = "{package["version"]}"'),
            before_cargo,
        )
        self.assertEqual((fixture / f"admin/release-notes/release-notes-v{package['version']}.md").read_text(), before_notes)
        snapshot = {path: path.read_bytes() for path in planned}
        with self.assertRaises(FileNotFoundError):
            release_version.updates(fixture, fixture / "admin", "9.8.6", "9.8.8")
        self.assertEqual({path: path.read_bytes() for path in planned}, snapshot)
        notes.write_text("# v9.8.7\n\n- Release changes.\n\nAdmin: `9.8.7`.\n\nBackend: `1.2.3`.\n")
        with self.assertRaisesRegex(ValueError, "release metadata"):
            release_version.updates(fixture, fixture / "admin", "9.8.6", "9.8.7")
        self.assertEqual({path: path.read_bytes() for path in planned}, snapshot)
        notes.write_text("# v9.8.7\n\n- Release changes.\n\nAdmin: `9.8.7`.\n\nBackend: `9.8.6`.\n")
        dockerfile = fixture / "Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace("ARG FAMILIA_VERSION=9.8.6", "ARG FAMILIA_VERSION=1.2.3"))
        self.assertNotEqual(release_version.updates(fixture, fixture / "admin", "9.8.6", "9.8.7")[dockerfile], dockerfile.read_text())


if __name__ == "__main__":
    unittest.main()
