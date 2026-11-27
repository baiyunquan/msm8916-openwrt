#!/usr/bin/env python3
"""Validate and stabilize HMUF02V5 OpenWrt build intermediates."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


STABLE_SOURCE_MTIME = 946684800  # 2000-01-01 UTC
STATE_FILE = ".hmuf02v5-cache-state"
SKIP_DIRS = {".git", "build_dir", "staging_dir", "dl", "bin", "tmp", "logs", "__pycache__"}
PROJECT_INPUTS = (
    "version",
    "devenv/.env",
    "devenv/dependencies.sh",
    "devenv/Dockerfile",
    "devenv/docker-compose.yml",
    "build.sh",
    "scripts/openwrt-prepare.sh",
    "scripts/configure-openwrt-ccache.sh",
    "scripts/openwrt_intermediate_cache.py",
    "diffconfigs/hmuf02v5",
    "msm89xx",
    "packages",
    "openwrt-overlay",
    "openwrt-patches",
)


def _hash_paths(root: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        path = root / relative
        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = []
            for current, dirs, names in os.walk(path):
                dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
                files.extend(Path(current) / name for name in sorted(names) if name != ".git")
        else:
            continue

        for file_path in sorted(files):
            if not file_path.is_file():
                continue
            digest.update(file_path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            with file_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_diff_sha256(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "diff", "--binary", "HEAD"],
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def _feed_state(openwrt_dir: Path) -> dict[str, dict[str, str]]:
    feeds_dir = openwrt_dir / "feeds"
    if not feeds_dir.is_dir():
        return {}
    state = {}
    for feed in sorted(feeds_dir.iterdir()):
        if feed.is_dir() and (feed / ".git").exists():
            state[feed.name] = {
                "head": _git_head(feed),
                "diff_sha256": _git_diff_sha256(feed),
            }
    return state


def cache_fingerprint(openwrt_dir: Path) -> dict[str, object]:
    repo_dir = Path(__file__).resolve().parents[1]
    config_path = openwrt_dir / ".config"
    if not config_path.is_file():
        raise FileNotFoundError(f"OpenWrt config not found: {config_path}")
    return {
        "schema": 1,
        "openwrt_head": _git_head(openwrt_dir),
        "openwrt_diff_sha256": _git_diff_sha256(openwrt_dir),
        "feed_state": _feed_state(openwrt_dir),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "project_inputs_sha256": _hash_paths(repo_dir, PROJECT_INPUTS),
        "live_project_sources_sha256": _hash_paths(
            openwrt_dir, ("target/linux/msm89xx", "package/msm8916")
        ),
    }


def normalize_source_mtimes(openwrt_dir: Path) -> None:
    """Give unchanged source trees stable timestamps so OpenWrt stamps match."""
    openwrt_dir = openwrt_dir.resolve()
    for current, dirs, names in os.walk(openwrt_dir):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        for name in names:
            path = Path(current) / name
            try:
                mode = path.stat(follow_symlinks=False).st_mode
                if stat.S_ISREG(mode) and path.name != STATE_FILE:
                    os.utime(
                        path,
                        (STABLE_SOURCE_MTIME, STABLE_SOURCE_MTIME),
                        follow_symlinks=False,
                    )
            except FileNotFoundError:
                continue


def prepare(openwrt_dir: Path) -> None:
    openwrt_dir = openwrt_dir.resolve()
    state_path = openwrt_dir / STATE_FILE
    build_dir = openwrt_dir / "build_dir"
    target_staging = openwrt_dir / "staging_dir/target-aarch64_generic_musl"

    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            current = cache_fingerprint(openwrt_dir)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            cache_matches = False
            print(f"Could not validate saved HMUF02V5 cache state: {error}")
        else:
            cache_matches = previous == current

        if not cache_matches:
            shutil.rmtree(build_dir, ignore_errors=True)
            shutil.rmtree(target_staging, ignore_errors=True)
            state_path.unlink(missing_ok=True)
            print("Discarded stale HMUF02V5 build intermediates; ccache remains usable.")
        else:
            print("HMUF02V5 build intermediates match the current OpenWrt, feeds, and config.")
    else:
        if build_dir.exists() or target_staging.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
            shutil.rmtree(target_staging, ignore_errors=True)
            print("Discarded HMUF02V5 intermediates without a state file; ccache remains usable.")
        else:
            print("No saved HMUF02V5 cache state; preparing a fresh intermediate cache.")

    # Some package host tools (notably luci-base's po2lmo/jsmin) compile in
    # feeds/*/src rather than HOST_BUILD_DIR. A fresh checkout loses those
    # binaries while restored .built stamps incorrectly skip Host/Compile.
    # Rebuild package host tools as a coherent build/staging pair. Keep the
    # target/kernel intermediates and the separate base toolchain/host cache.
    shutil.rmtree(build_dir / "hostpkg", ignore_errors=True)
    shutil.rmtree(openwrt_dir / "staging_dir/hostpkg", ignore_errors=True)
    print("Reset package host tools for the fresh runner checkout.")

    normalize_source_mtimes(openwrt_dir)


def record(openwrt_dir: Path) -> None:
    openwrt_dir = openwrt_dir.resolve()
    state_path = openwrt_dir / STATE_FILE
    state_path.write_text(
        json.dumps(cache_fingerprint(openwrt_dir), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Recorded HMUF02V5 intermediate state in {state_path}.")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"prepare", "record"}:
        print(f"usage: {argv[0]} prepare|record OPENWRT_DIR", file=sys.stderr)
        return 2
    openwrt_dir = Path(argv[2])
    if argv[1] == "prepare":
        prepare(openwrt_dir)
    else:
        record(openwrt_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
