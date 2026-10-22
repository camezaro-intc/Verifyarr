#!/usr/bin/env python3
"""Standalone diagnostic CLI to simulate a Radarr webhook and verify dashboard persistence.

Usage:
    python scripts/simulate_event.py \\
        --path "E:\\Temp_Flex_Media\\Movies\\Test\\movie.mkv" \\
        --title "Test Movie" \\
        --runtime 120 \\
        --expected 120
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def build_payload(
    file_path: str,
    title: str,
    expected_runtime: int,
    event_type: str = "Download",
    year: int = 2026,
    language: str = "English",
    bad_file: bool = False,
    download_id: str = "4a76a8d6778f691d1e4e7195c64ef51381283626",
) -> dict[str, Any]:
    norm_event = event_type.strip().lower()
    clean_title = title.replace(" ", ".")
    if norm_event == "grab":
        ext = ".7z" if bad_file else ".mkv"
        rel_title = f"{clean_title}.S01E01.720p.HDTV.x264-FLUX{ext}"
        return {
            "eventType": "Grab",
            "instanceName": "Sonarr (Simulate CLI)",
            "series": {
                "id": 999999,
                "title": title,
                "path": f"C:/Media/TV/{title}",
                "tvdbId": 12345,
            },
            "episodes": [
                {
                    "id": 888888,
                    "episodeNumber": 1,
                    "seasonNumber": 1,
                    "title": "Pilot",
                }
            ],
            "release": {
                "releaseTitle": rel_title,
                "indexer": "VerifyarrCLI",
                "size": 524288000,
            },
            "downloadClient": "qBittorrent",
            "downloadClientType": "qBittorrent",
            "downloadId": download_id,
        }

    path_obj = Path(file_path) if file_path else Path(f"{title}.mkv")
    folder_path = str(path_obj.parent) if path_obj.suffix else str(path_obj)
    rel_path = path_obj.name if path_obj.suffix else ""
    release_title = path_obj.stem or title.replace(" ", ".")
    return {
        "eventType": event_type,
        "instanceName": "Radarr (Simulate CLI)",
        "isUpgrade": False,
        "movie": {
            "id": 999999,
            "title": title,
            "year": year,
            "runtime": expected_runtime,
            "tmdbId": 100000,
            "imdbId": "tt0000000",
            "originalLanguage": {"id": 1, "name": language},
            "folderPath": folder_path,
        },
        "movieFile": {
            "id": 888888,
            "relativePath": rel_path,
            "path": str(path_obj),
            "sceneName": release_title,
            "quality": "Bluray-1080p",
        },
        "release": {
            "releaseTitle": release_title,
            "indexer": "VerifyarrCLI",
            "quality": "Bluray-1080p",
        },
    }


def post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return exc.code, body


def get_text(url: str, timeout: float = 10.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def find_dashboard_entry(dashboard_html: str, title: str, release_title: str) -> str | None:
    """Return a short summary if ``title`` or ``release_title`` appears on the dashboard."""
    escaped_title = html.escape(title)
    escaped_release = html.escape(release_title)
    rows = re.findall(r"<tr id=\"row-\d+\">.*?</tr>", dashboard_html, flags=re.DOTALL)
    for row in rows:
        if escaped_title in row or escaped_release in row or title in row or release_title in row:
            badge_match = re.search(r'<span class="badge\s+([A-Z_]+)">', row)
            badge = badge_match.group(1) if badge_match else "UNKNOWN"
            reason_matches = re.findall(r"<li>(.*?)</li>", row, flags=re.DOTALL)
            first_reason = html.unescape(reason_matches[0].strip()) if reason_matches else "OK"
            return f"status={badge} | detail={first_reason}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a synthetic Servarr webhook to Verifyarr and check the dashboard."
    )
    parser.add_argument(
        "--path",
        default="",
        help="Path to the media file (or movie folder) to verify",
    )
    parser.add_argument(
        "--type",
        dest="event_type",
        default="Download",
        help="Servarr eventType to simulate (Download, Upgrade, Grab, Test; default: Download)",
    )
    parser.add_argument(
        "--event-type",
        dest="event_type",
        help="Alias for --type",
    )
    parser.add_argument(
        "--bad-file",
        action="store_true",
        help="Simulate a bad/malicious release (e.g. .7z archive in torrent or bad media)",
    )
    parser.add_argument(
        "--hash",
        default="4a76a8d6778f691d1e4e7195c64ef51381283626",
        help="Torrent downloadId / info-hash for grab simulations",
    )
    parser.add_argument(
        "--title",
        default="Test Movie",
        help="Movie/Series title to send in the webhook (default: 'Test Movie')",
    )
    parser.add_argument(
        "--runtime",
        type=int,
        default=120,
        help="Runtime in minutes (used as expected runtime unless --expected is set)",
    )
    parser.add_argument(
        "--expected",
        type=int,
        default=None,
        help="Expected runtime in minutes reported by Servarr (overrides --runtime)",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:7979",
        help="Base URL of Verifyarr (default: http://localhost:7979)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=15.0,
        help="Seconds to poll the dashboard for verification completion (default: 15)",
    )
    args = parser.parse_args(argv)

    if args.event_type.strip().lower() != "grab" and not args.path:
        print("ERROR: --path is required for import/download events.")
        return 1

    expected_runtime = args.expected if args.expected is not None else args.runtime
    base_url = args.url.rstrip("/")
    webhook_url = f"{base_url}/api/v1/webhook"
    dashboard_url = f"{base_url}/"

    payload = build_payload(
        file_path=args.path,
        title=args.title,
        expected_runtime=expected_runtime,
        event_type=args.event_type,
        bad_file=args.bad_file,
        download_id=args.hash,
    )
    release_title = payload["release"]["releaseTitle"]

    print(f"Sending {args.event_type!r} webhook to {webhook_url}")
    print(f"  Title:            {args.title}")
    if args.event_type.strip().lower() == "grab":
        print(f"  Release:          {release_title}")
        print(f"  Download ID:      {args.hash}")
        print(f"  Bad File Flag:    {args.bad_file}")
    else:
        print(f"  Path:             {args.path}")
        print(f"  Expected Runtime: {expected_runtime} min")

    try:
        status_code, response_body = post_json(webhook_url, payload)
    except urllib.error.URLError as exc:
        print(f"ERROR: Could not connect to Verifyarr at {webhook_url}: {exc}")
        return 1

    print(f"\nWebhook HTTP Status: {status_code}")
    print(f"Webhook Response:    {response_body}")

    if status_code not in (200, 202):
        print("Webhook was not accepted by Verifyarr.")
        return 1

    if args.event_type.strip().lower() == "test":
        return 0

    print(f"\nPolling dashboard ({dashboard_url}) for '{args.title}' ...")
    deadline = time.monotonic() + max(1.0, args.wait)
    while time.monotonic() < deadline:
        try:
            dash_status, dash_html = get_text(dashboard_url)
            if dash_status == 200:
                summary = find_dashboard_entry(dash_html, args.title, release_title)
                if summary:
                    print(f"SUCCESS: Entry landed on dashboard -> {summary}")
                    return 0
        except urllib.error.URLError:
            pass
        time.sleep(0.5)

    print("WARNING: Entry did not appear on the dashboard within the wait window.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
