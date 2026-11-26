#!/usr/bin/env python3
"""Tests for the OpenWrt Actions cache provider and pruning policy."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import action_cache  # noqa: E402
import openwrt_intermediate_cache  # noqa: E402


class CcacheProviderTests(unittest.TestCase):
    def test_hmuf_is_preferred_even_when_it_is_not_first(self):
        self.assertEqual(
            action_cache.choose_ccache_provider(["ufi003", "hmuf02v5", "jz02"]),
            "hmuf02v5",
        )

    def test_first_selected_board_provides_ccache_when_hmuf_is_absent(self):
        self.assertEqual(
            action_cache.choose_ccache_provider(["ufi003", "jz02"]),
            "ufi003",
        )

    def test_empty_board_list_is_rejected(self):
        with self.assertRaises(ValueError):
            action_cache.choose_ccache_provider([])

    def test_board_matrix_json_is_valid(self):
        boards = ["hmuf02v5", "ufi103s-v05"]
        self.assertEqual(json.loads(action_cache.format_boards_json(boards)), boards)


class OpenWrtCcacheConfigTests(unittest.TestCase):
    @staticmethod
    def _git_repo(path: Path, file_name: str, contents: str):
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Cache Test"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "cache-test@example.invalid"],
            check=True,
        )
        (path / file_name).write_text(contents, encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", file_name], check=True)
        subprocess.run(
            ["git", "-C", str(path), "commit", "--quiet", "-m", "cache fixture"],
            check=True,
        )

    def test_configurator_replaces_existing_and_disabled_values(self):
        helper = Path(__file__).resolve().parents[1] / "configure-openwrt-ccache.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / ".config"
            config.write_text(
                "# CONFIG_DEVEL is not set\n"
                "# CONFIG_CCACHE is not set\n"
                'CONFIG_CCACHE_DIR=""\n'
                "CONFIG_TARGET=y\n",
                encoding="utf-8",
            )

            subprocess.run(["sh", str(helper), str(config)], check=True)

            lines = config.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines.count("CONFIG_DEVEL=y"), 1)
            self.assertEqual(lines.count("CONFIG_CCACHE=y"), 1)
            self.assertEqual(lines.count('CONFIG_CCACHE_DIR="/repo/ccache"'), 1)
            self.assertIn("CONFIG_TARGET=y", lines)
            self.assertFalse(any(line.startswith("# CONFIG_CCACHE") for line in lines))

    def test_source_mtime_normalization_leaves_build_outputs_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "package" / "example" / "Makefile"
            output = root / "build_dir" / "artifact.o"
            source.parent.mkdir(parents=True)
            output.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            output.write_text("output", encoding="utf-8")
            original_output_mtime = output.stat().st_mtime

            openwrt_intermediate_cache.normalize_source_mtimes(root)

            self.assertEqual(
                source.stat().st_mtime,
                openwrt_intermediate_cache.STABLE_SOURCE_MTIME,
            )
            self.assertEqual(output.stat().st_mtime, original_output_mtime)

    def test_matching_state_preserves_intermediates_and_feed_change_discards_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "openwrt"
            self._git_repo(root, ".config", "CONFIG_TARGET=y\n")
            feed = root / "feeds" / "packages"
            self._git_repo(feed, "Makefile", "PKG_NAME:=fixture\n")
            state_file = root / openwrt_intermediate_cache.STATE_FILE
            state_file.write_text(
                json.dumps(openwrt_intermediate_cache.cache_fingerprint(root)),
                encoding="utf-8",
            )
            build_dir = root / "build_dir"
            staging_dir = root / "staging_dir" / "target-aarch64_generic_musl"
            build_dir.mkdir()
            staging_dir.mkdir(parents=True)
            (build_dir / "object.o").write_text("object", encoding="utf-8")
            (staging_dir / "package.apk").write_text("package", encoding="utf-8")

            openwrt_intermediate_cache.prepare(root)
            self.assertTrue((build_dir / "object.o").exists())
            self.assertTrue((staging_dir / "package.apk").exists())

            (feed / "Makefile").write_text("PKG_NAME:=changed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(feed), "add", "Makefile"], check=True)
            subprocess.run(
                ["git", "-C", str(feed), "commit", "--quiet", "-m", "feed update"],
                check=True,
            )

            openwrt_intermediate_cache.prepare(root)
            self.assertFalse(build_dir.exists())
            self.assertFalse(staging_dir.exists())

    def test_corrupt_state_discards_existing_intermediates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "openwrt"
            self._git_repo(root, ".config", "CONFIG_TARGET=y\n")
            state_file = root / openwrt_intermediate_cache.STATE_FILE
            state_file.write_text("not valid json", encoding="utf-8")
            build_dir = root / "build_dir"
            staging_dir = root / "staging_dir" / "target-aarch64_generic_musl"
            build_dir.mkdir()
            staging_dir.mkdir(parents=True)

            openwrt_intermediate_cache.prepare(root)

            self.assertFalse(build_dir.exists())
            self.assertFalse(staging_dir.exists())


class CachePruningTests(unittest.TestCase):
    def test_keeps_one_latest_cache_per_family_and_removes_legacy_caches(self):
        caches = [
            {"id": 1, "key": "openwrt-downloads-v1-a-1", "created_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "key": "openwrt-downloads-v1-a-2", "created_at": "2026-01-02T00:00:00Z"},
            {"id": 3, "key": "openwrt-ccache-v1-b-1", "created_at": "2026-01-01T00:00:00Z"},
            {"id": 4, "key": "openwrt-ccache-v1-b-2", "created_at": "2026-01-02T00:00:00Z"},
            {"id": 5, "key": "openwrt-hmuf02v5-v1-c-1", "created_at": "2026-01-02T00:00:00Z"},
            {"id": 6, "key": "openwrt-pr-cache-old", "created_at": "2025-12-31T00:00:00Z"},
            {"id": 7, "key": "unrelated-cache", "created_at": "2026-01-03T00:00:00Z"},
        ]

        keep, delete = action_cache.plan_prune(caches)

        self.assertEqual({cache["id"] for cache in keep}, {2, 4, 5, 7})
        self.assertEqual({cache["id"] for cache in delete}, {1, 3, 6})

    def test_preserves_legacy_download_cache_until_a_replacement_exists(self):
        caches = [
            {"id": 1, "key": "openwrt-pr-cache-old", "created_at": "2025-12-31T00:00:00Z"},
            {"id": 2, "key": "openwrt-ccache-v1-b-1", "created_at": "2026-01-01T00:00:00Z"},
        ]

        keep, delete = action_cache.plan_prune(caches)

        self.assertEqual({cache["id"] for cache in keep}, {1, 2})
        self.assertEqual(delete, [])

    def test_after_save_prune_replaces_only_downloads_and_legacy(self):
        caches = [
            {"id": 1, "key": "openwrt-downloads-v1-old"},
            {"id": 2, "key": "openwrt-ccache-v1-old"},
            {"id": 3, "key": "openwrt-hmuf02v5-v1-old"},
            {"id": 4, "key": "openwrt-pr-cache-old"},
            {"id": 5, "key": "unrelated-cache"},
            {"id": 6, "key": "openwrt-downloads-v1-new-run-7"},
        ]

        keep, delete = action_cache.plan_prune_after_save(
            caches, "openwrt-downloads-v1-new-run-7"
        )

        self.assertEqual({cache["id"] for cache in keep}, {2, 3, 5, 6})
        self.assertEqual({cache["id"] for cache in delete}, {1, 4})

    def test_after_save_prune_preserves_old_cache_if_upload_is_missing(self):
        caches = [
            {"id": 1, "key": "openwrt-ccache-v1-old"},
            {"id": 2, "key": "unrelated-cache"},
        ]

        keep, delete = action_cache.plan_prune_after_save(
            caches, "openwrt-ccache-v1-new-run-7"
        )

        self.assertEqual(keep, caches)
        self.assertEqual(delete, [])


if __name__ == "__main__":
    unittest.main()
