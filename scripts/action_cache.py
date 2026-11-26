#!/usr/bin/env python3
"""Shared cache policy for GitHub Actions OpenWrt builds."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CACHE_FAMILIES = (
    "openwrt-downloads-v1-",
    "openwrt-ccache-v1-",
    "openwrt-hmuf02v5-v1-",
)
CACHE_QUOTA_BYTES = 10_000_000_000
LEGACY_CACHE_PREFIXES = ("openwrt-cache-", "openwrt-pr-cache-")


def choose_ccache_provider(boards: list[str]) -> str:
    """Prefer HMUF02V5; otherwise use the first requested board."""
    if not boards:
        raise ValueError("at least one board is required")
    if "hmuf02v5" in boards:
        return "hmuf02v5"
    return boards[0]


def format_boards_json(boards: list[str]) -> str:
    """Serialize selected boards as a JSON matrix for GitHub Actions."""
    if not boards:
        raise ValueError("at least one board is required")
    return json.dumps(boards, separators=(",", ":"))


def _created_at(cache: dict) -> datetime:
    value = cache.get("created_at", "")
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def plan_prune(caches: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep the newest cache in each family and remove superseded OpenWrt caches.

    Legacy download/toolchain caches remain until a replacement downloads cache
    exists, so a failed first migration cannot discard the only warm cache.
    Unrelated repository caches are left alone.
    """
    latest_by_prefix: dict[str, dict] = {}
    for prefix in CACHE_FAMILIES:
        matches = [cache for cache in caches if cache.get("key", "").startswith(prefix)]
        if matches:
            latest_by_prefix[prefix] = max(
                matches,
                key=lambda cache: (_created_at(cache), int(cache.get("id", 0))),
            )

    has_downloads_replacement = CACHE_FAMILIES[0] in latest_by_prefix
    keep: list[dict] = []
    delete: list[dict] = []

    for cache in caches:
        key = cache.get("key", "")
        prefix = next((item for item in CACHE_FAMILIES if key.startswith(item)), None)
        if prefix:
            if cache["id"] == latest_by_prefix[prefix]["id"]:
                keep.append(cache)
            else:
                delete.append(cache)
            continue

        is_legacy = key.startswith(LEGACY_CACHE_PREFIXES)
        if is_legacy and has_downloads_replacement:
            delete.append(cache)
        else:
            keep.append(cache)

    return keep, delete


def plan_prune_after_save(
    caches: list[dict], saved_key: str
) -> tuple[list[dict], list[dict]]:
    """Replace an older family only after its new exact key exists."""
    family = next((prefix for prefix in CACHE_FAMILIES if saved_key.startswith(prefix)), None)
    if family is None:
        raise ValueError(f"unknown OpenWrt cache family: {saved_key}")
    if not any(cache.get("key") == saved_key for cache in caches):
        return caches, []

    keep: list[dict] = []
    delete: list[dict] = []
    for cache in caches:
        key = cache.get("key", "")
        replace_family = key.startswith(family) and key != saved_key
        retire_legacy = family == CACHE_FAMILIES[0] and key.startswith(LEGACY_CACHE_PREFIXES)
        (delete if replace_family or retire_legacy else keep).append(cache)
    return keep, delete


def _api_request(method: str, url: str, token: str):
    request = Request(
        url,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except (HTTPError, URLError) as error:
        detail = getattr(error, "read", lambda: b"")().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API {method} {url} failed: {error} {detail}") from error


def _list_caches(api_url: str, repository: str, token: str) -> list[dict]:
    result: list[dict] = []
    page = 1
    while True:
        query = urlencode({"per_page": 100, "page": page})
        url = f"{api_url}/repos/{repository}/actions/caches?{query}"
        payload = _api_request("GET", url, token)
        batch = payload.get("actions_caches", [])
        result.extend(batch)
        if len(batch) < 100:
            return result
        page += 1


def _github_cache_context() -> tuple[str, str, str]:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not repository or not token:
        raise RuntimeError("GITHUB_REPOSITORY and GH_TOKEN are required")
    return repository, token, api_url


def _delete_caches(
    caches: list[dict], *, repository: str, token: str, api_url: str
) -> None:
    for cache in caches:
        url = f"{api_url}/repos/{repository}/actions/caches/{cache['id']}"
        _api_request("DELETE", url, token)
        print(f"Deleted cache {cache['id']}: {cache.get('key', '')}")


def prune_github_caches() -> None:
    repository, token, api_url = _github_cache_context()
    caches = _list_caches(api_url, repository, token)
    keep, delete = plan_prune(caches)
    print(f"OpenWrt cache cleanup: keeping {len(keep)}, deleting {len(delete)}")
    _delete_caches(delete, repository=repository, token=token, api_url=api_url)
    managed = [
        cache
        for cache in keep
        if cache.get("key", "").startswith(CACHE_FAMILIES)
    ]
    managed_bytes = sum(int(cache.get("size_in_bytes", 0)) for cache in managed)
    print(
        "Latest managed cache total: "
        f"{managed_bytes:,} bytes ({managed_bytes / (1024 ** 3):.2f} GiB)"
    )
    if managed_bytes > CACHE_QUOTA_BYTES:
        print(
            "::warning::OpenWrt cache families exceed the 10 GB Actions cache quota; "
            "GitHub may evict one of the three managed entries."
        )
    for prefix in CACHE_FAMILIES:
        current = next(
            (cache for cache in keep if cache.get("key", "").startswith(prefix)),
            None,
        )
        print(f"Latest {prefix}: {current.get('key') if current else '(not created yet)'}")


def prune_after_save(saved_key: str) -> None:
    repository, token, api_url = _github_cache_context()
    caches = _list_caches(api_url, repository, token)
    keep, delete = plan_prune_after_save(caches, saved_key)
    print(
        f"OpenWrt cache post-save cleanup for {saved_key}: "
        f"keeping {len(keep)}, deleting {len(delete)}"
    )
    _delete_caches(delete, repository=repository, token=token, api_url=api_url)


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "provider":
        try:
            print(choose_ccache_provider(argv[2:]))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        return 0
    if len(argv) >= 2 and argv[1] == "boards-json":
        try:
            print(format_boards_json(argv[2:]))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        return 0
    if len(argv) == 2 and argv[1] == "prune":
        prune_github_caches()
        return 0
    if len(argv) == 3 and argv[1] == "prune-after-save":
        try:
            prune_after_save(argv[2])
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        return 0
    print(
        f"usage: {argv[0]} provider BOARD... | boards-json BOARD... | "
        "prune | prune-after-save CACHE_KEY",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
