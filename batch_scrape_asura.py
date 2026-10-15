#!/usr/bin/env python3
"""
Batch scrape all missing Asura series and sync to KV.
"""

import asyncio
import json
import sys
import urllib.request
import httpx

WORKER_BASE = "https://manhwa-kv-proxy.dexlot8.workers.dev"
ASURA_API = "https://api.asurascans.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DELAY = 0.3

MISSING_SLUGS = [
    "echoes-of-the-reverse-planet",
    "genius-prismatic-mage",
    "hellogin",
    "pick-up-a-grimoire-become-a-mage",
    "regressor-instruction-manual",
    "return-of-the-disaster-class-hero",
    "steel-eating-player",
    "surviving-the-game-as-a-barbarian",
    "the-academys-sashimi-sword-master",
    "the-academy-s-weapon-replicator",
    "the-demon-god",
    "the-former-supreme",
    "the-heavenly-demon-wants-a-quiet-life",
    "the-knight-king-who-returned-with-a-god",
    "the-s-classes-that-i-raised",
    "villain-to-kill",
    "war-of-extinction",
]


async def _get(client, url, timeout=30):
    try:
        r = await client.get(url, timeout=timeout)
        if r.status_code == 200:
            return r
        return r
    except Exception as e:
        print(f"    Error: {e}")
        return None


async def scrape_series(client, slug):
    """Scrape one series: metadata + all chapters with images."""
    print(f"\n{'='*60}")
    print(f"Scraping: {slug}")
    print(f"{'='*60}")

    # 1) Series metadata
    r = await _get(client, f"{ASURA_API}/api/series/{slug}")
    if not r or r.status_code != 200:
        print(f"  ERROR: Could not fetch series info")
        return None

    series_info = r.json().get("series", {})
    title = series_info.get("title", slug)
    cover = series_info.get("cover", "")
    status = series_info.get("status", "ongoing")
    tags = series_info.get("tags", [])
    description = series_info.get("description", "")
    author = series_info.get("author", "")
    artist = series_info.get("artist", "")

    print(f"  Title: {title}")
    print(f"  Status: {status}")
    print(f"  Tags: {tags}")

    # 2) Chapter list
    r = await _get(client, f"{ASURA_API}/api/series/{slug}/chapters")
    if not r or r.status_code != 200:
        print(f"  ERROR: Could not fetch chapters")
        return None

    chapters_data = r.json().get("data", [])
    print(f"  Chapters: {len(chapters_data)}")

    # 3) Fetch images for each chapter
    chapters = []
    for i, ch in enumerate(chapters_data):
        ch_num = ch.get("number")
        ch_title = ch.get("title") or f"Chapter {ch_num:g}"

        r = await _get(client, f"{ASURA_API}/api/series/{slug}/chapters/{ch_num:g}")
        if not r or r.status_code != 200:
            print(f"  [{i+1}/{len(chapters_data)}] Ch {ch_num:g} - FAILED")
            chapters.append({"number": ch_num, "title": ch_title, "image_urls": []})
            continue

        ch_data = r.json().get("data", {}).get("chapter", {})
        pages = ch_data.get("pages", [])
        images = [p["url"] for p in pages if p.get("url")]
        published_at = ch_data.get("published_at", "")

        chapters.append({
            "number": ch_num,
            "title": ch_title,
            "image_urls": images,
            "added_at": published_at,
        })

        if (i + 1) % 20 == 0 or i == len(chapters_data) - 1:
            print(f"  [{i+1}/{len(chapters_data)}] Ch {ch_num:g} - {len(images)} images")

        await asyncio.sleep(DELAY)

    total_images = sum(len(ch["image_urls"]) for ch in chapters)
    print(f"  Total images: {total_images}")

    return {
        "slug": slug,
        "title": title,
        "cover_url": cover,
        "status": status,
        "tags": tags,
        "description": description,
        "author": author,
        "artist": artist,
        "chapter_count": len(chapters),
        "chapters": chapters,
    }


async def sync_to_kv(bundle):
    """POST series bundle to worker sync endpoint."""
    payload = {"series": bundle}
    req = urllib.request.Request(
        f"{WORKER_BASE}/sync?token=dexmanhwa-sync-2026",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read().decode())
    return result


async def main():
    # Load existing all_series to append new ones
    req = urllib.request.Request(
        f"{WORKER_BASE}/all_series",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        all_series_data = json.loads(r.read().decode())

    existing_series = all_series_data.get("series", [])
    existing_slugs = {s["slug"] for s in existing_series}
    print(f"Existing series in KV: {len(existing_series)}")

    # Scrape all missing series
    new_bundles = []
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for slug in MISSING_SLUGS:
            if slug in existing_slugs:
                print(f"\nSkipping {slug} (already exists)")
                continue

            bundle = await scrape_series(client, slug)
            if bundle:
                new_bundles.append(bundle)
                # Sync each series immediately
                result = await sync_to_kv(bundle)
                print(f"  Synced: {result}")

    # Update all_series index
    if new_bundles:
        print(f"\n{'='*60}")
        print(f"Updating all_series index with {len(new_bundles)} new series...")
        print(f"{'='*60}")

        for bundle in new_bundles:
            # Check if already in list
            existing = next((s for s in existing_series if s["slug"] == bundle["slug"]), None)
            if existing:
                # Update existing entry
                existing["title"] = bundle["title"]
                existing["cover_url"] = bundle["cover_url"]
                existing["chapter_count"] = bundle["chapter_count"]
                existing["status"] = bundle["status"]
                existing["tags"] = bundle["tags"]
            else:
                # Add new entry
                existing_series.append({
                    "slug": bundle["slug"],
                    "title": bundle["title"],
                    "cover_url": bundle["cover_url"],
                    "chapter_count": bundle["chapter_count"],
                    "status": bundle["status"],
                    "tags": bundle["tags"],
                })

        # Write updated all_series
        payload = {"all_series": {"series": existing_series}}
        req = urllib.request.Request(
            f"{WORKER_BASE}/sync?token=dexmanhwa-sync-2026",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read().decode())
        print(f"  all_series updated: {result}")
        print(f"  Total series now: {len(existing_series)}")

    print(f"\n{'='*60}")
    print(f"DONE - {len(new_bundles)} new series scraped and synced")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
