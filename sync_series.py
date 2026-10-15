#!/usr/bin/env python3
"""
Sync ManhuaPlus series images to Cloudflare KV.

Reads existing series bundle from KV (chapter structure already there),
scrapes images for each chapter from ManhuaPlus, and PUTs the full
updated bundle back to KV.
"""

import asyncio
import json
import re
import sys
import urllib.request
import httpx

WORKER_BASE = "https://manhwa-kv-proxy.dexlot8.workers.dev"
MANHUAPLUS_BASE = "https://manhuaplus.org"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DELAY = 0.6


async def _get(client, url, retries=3):
    for attempt in range(retries):
        try:
            r = await client.get(url, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            return r
        except Exception:
            if attempt == retries - 1:
                return None
            await asyncio.sleep(3 * (attempt + 1))
    return None


async def get_chapter_images(client, slug, chapter_url):
    """Fetch chapter page, extract CHAPTER_ID, call ajax, sort by data-index."""
    r = await _get(client, chapter_url)
    if not r or r.status_code != 200:
        return []

    m = re.search(r'const CHAPTER_ID = (\d+);', r.text)
    if not m:
        return []

    chapter_id = m.group(1)
    await asyncio.sleep(DELAY)

    ajax_r = await _get(client, f"{MANHUAPLUS_BASE}/ajax/image/list/chap/{chapter_id}")
    if not ajax_r or ajax_r.status_code != 200:
        return []

    try:
        data = ajax_r.json()
    except Exception:
        return []

    if not data.get("status"):
        return []

    html = data.get("html", "")
    indexed = re.findall(
        r'data-index="(\d+)".*?src="(https://cdn\.manhuaplus\.cc/[^"]+)"',
        html,
        re.DOTALL,
    )
    if indexed:
        return [url for _, url in sorted(indexed, key=lambda x: int(x[0]))]
    return list(dict.fromkeys(re.findall(r'src="(https://cdn\.manhuaplus\.cc/[^"]+)"', html)))


async def sync_series(slug, max_chapters=None):
    """Sync one series: fetch existing bundle, scrape images, PUT back."""
    print(f"\n{'='*60}")
    print(f"Syncing: {slug}")
    print(f"{'='*60}")

    # 1) Fetch existing series bundle from KV
    req = urllib.request.Request(
        f"{WORKER_BASE}/series:id:{slug}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        bundle = json.loads(r.read().decode())

    chapters = bundle.get("chapters", [])
    print(f"  Existing chapters: {len(chapters)}")

    # Map our slug to ManhuaPlus slug if different
    SOURCE_SLUG_MAP = {
        "serpent-ancestor": "snake-immortal-the-tale-of-a-snake-s-cultivation-to-immortality",
    }
    source_slug = SOURCE_SLUG_MAP.get(slug, slug)
    print(f"  Our slug: {slug}")
    print(f"  Source slug: {source_slug}")
    
    if max_chapters:
        chapters = chapters[:max_chapters]

    # 2) Scrape images for each chapter
    async with httpx.AsyncClient(
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for i, ch in enumerate(chapters):
            ch_num = ch.get("number")
            ch_url = f"{MANHUAPLUS_BASE}/manga/{source_slug}/chapter-{ch_num:g}"
            
            print(f"  [{i+1}/{len(chapters)}] Chapter {ch_num:g}...", end=" ", flush=True)
            images = await get_chapter_images(client, slug, ch_url)
            ch["image_urls"] = images
            print(f"{len(images)} images")

    # 3) POST updated bundle to worker sync endpoint
    sync_payload = {
        "series": bundle,  # Write full bundle to series:{slug}
    }
    
    req = urllib.request.Request(
        f"{WORKER_BASE}/sync?token=dexmanhwa-sync-2026",
        data=json.dumps(sync_payload).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read().decode())
    
    print(f"\n  Sync result: {result}")
    
    # Also save to local file for reference
    output_path = f"/home/hermes/manhwa-scraper/{slug}_synced.json"
    with open(output_path, "w") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)
    print(f"  Saved to: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sync_series.py <slug> [max_chapters]")
        sys.exit(1)
    
    slug = sys.argv[1]
    max_ch = int(sys.argv[2]) if len(sys.argv) > 2 else None
    
    asyncio.run(sync_series(slug, max_ch))
