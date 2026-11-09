"""Pinned cache reuse and offline failures without external network access."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup-toolchain.py"
spec = importlib.util.spec_from_file_location("bendspec_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name) / "cache"
        self.cache.mkdir()
        self.data = b"pinned test source"
        self.files = {"bend.ts": hashlib.sha256(self.data).hexdigest()}

    def test_valid_cache_never_downloads(self):
        (self.cache / "bend.ts").write_bytes(self.data)
        with patch.object(setup.urllib.request, "urlopen", side_effect=AssertionError("unexpected network")):
            for offline in (False, True):
                setup.prepare_sources(self.cache, "a" * 40, self.files, offline)

    def test_offline_missing_or_corrupt_cache_rejects_without_network(self):
        with patch.object(setup.urllib.request, "urlopen", side_effect=AssertionError("unexpected network")):
            for data in (None, b"tampered"):
                with self.subTest(data=data):
                    if data is not None:
                        (self.cache / "bend.ts").write_bytes(data)
                    with self.assertRaisesRegex(RuntimeError, "offline cache"):
                        setup.prepare_sources(self.cache, "a" * 40, self.files, offline=True)

    def test_offline_symlink_rejects_even_when_bytes_match(self):
        target = self.cache.parent / "source"
        target.write_bytes(self.data)
        (self.cache / "bend.ts").symlink_to(target)
        with patch.object(setup.urllib.request, "urlopen", side_effect=AssertionError("unexpected network")):
            with self.assertRaisesRegex(RuntimeError, "offline cache"):
                setup.prepare_sources(self.cache, "a" * 40, self.files, offline=True)

    def test_download_requires_the_pinned_revision_and_digest(self):
        with patch.object(setup.urllib.request, "urlopen", return_value=io.BytesIO(self.data)) as download:
            setup.prepare_sources(self.cache, "a" * 40, self.files)
        download.assert_called_once_with(
            "https://raw.githubusercontent.com/bendlang/bend/" + "a" * 40 + "/bend2/bend.ts", timeout=30)
        self.assertEqual((self.cache / "bend.ts").read_bytes(), self.data)

    def test_bad_download_does_not_replace_cached_bytes(self):
        path = self.cache / "bend.ts"
        path.write_bytes(b"old cache")
        with patch.object(setup.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong source")):
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                setup.prepare_sources(self.cache, "a" * 40, self.files)
        self.assertEqual(path.read_bytes(), b"old cache")
