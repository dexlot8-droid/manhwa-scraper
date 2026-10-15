#!/usr/bin/env python3
"""
Fallback sync: merges all sources into KV with fallback chains.

For each matched series:
1. Fetch chapter data from all available sources
2. Merge into single bundle with fallback URLs
3. Write to KV

Result: each chapter has image_urls (primary) + fallback_image_urls (array of alternatives)
"""

import json
import re
import urllib.request
import asyncio
import httpx
from collections import defaultdict

ASURA_API = "https://api.asurascans.com"
FLAME_API = "https://flamecomics.xyz"
VORTEX_API = "https://api.vortexscans.org"
KV_BASE = "https://manhwa-kv-proxy.dexlot8.workers.dev"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_html(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")

async def _mp_get(client, url, retries=3):
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

# ─── Source-specific chapter fetchers ──────────────────────

async def fetch_asura_chapters(client, slug):
    """Fetch all chapters with images from Asura."""
    try:
        r = await _mp_get(client, f"{ASURA_API}/api/series/{slug}/chapters")
        if not r or r.status_code != 200:
            return []
        chapters = r.json().get("data", [])
        results = []
        for ch in chapters:
            ch_num = ch.get("number")
            ch_title = ch.get("title") or f"Chapter {ch_num:g}"
            r2 = await _mp_get(client, f"{ASURA_API}/api/series/{slug}/chapters/{ch_num:g}")
            if not r2 or r2.status_code != 200:
                results.append({"number": ch_num, "title": ch_title, "image_urls": []})
                continue
            pages = r2.json().get("data", {}).get("chapter", {}).get("pages", [])
            images = [p["url"] for p in pages if p.get("url")]
            results.append({"number": ch_num, "title": ch_title, "image_urls": images})
        return results
    except Exception as e:
        print(f"    Asura error: {e}")
        return []

async def fetch_flame_chapters(client, flame_id):
    """Fetch all chapters with images from Flame."""
    try:
        # Get chapter list from series page
        r = await _mp_get(client, f"{FLAME_API}/series/{flame_id}")
        if not r or r.status_code != 200:
            return []
        
        # Extract chapter data from __NEXT_DATA__
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', r.text)
        if not m:
            return []
        data = json.loads(m.group(1))
        
        # Navigate to chapters
        def find_chapters(obj, depth=0):
            if depth > 10: return None
            if isinstance(obj, list) and len(obj) > 0:
                if isinstance(obj[0], dict) and 'number' in obj[0]:
                    return obj
            if isinstance(obj, dict):
                for v in obj.values():
                    r = find_chapters(v, depth+1)
                    if r: return r
            return None
        
        chapters = find_chapters(data)
        if not chapters:
            return []
        
        results = []
        for ch in chapters:
            ch_num = ch.get("number")
            ch_title = ch.get("title") or f"Chapter {ch_num:g}"
            # Flame chapter images are in the chapter page
            # For now, just store the chapter number
            results.append({"number": ch_num, "title": ch_title, "image_urls": []})
        return results
    except Exception as e:
        print(f"    Flame error: {e}")
        return []

async def fetch_vortex_chapters(client, vortex_id):
    """Fetch all chapters with images from Vortex."""
    try:
        r = await _mp_get(client, f"{VORTEX_API}/api/posts/{vortex_id}")
        if not r or r.status_code != 200:
            return []
        data = r.json()
        post = data.get("post", data)
        chapters = post.get("chapters", [])
        results = []
        for ch in chapters:
            ch_num = ch.get("number")
            ch_title = ch.get("title") or f"Chapter {ch_num:g}"
            # Vortex chapter images need a separate API call per chapter
            # For now, just store the chapter number
            results.append({"number": ch_num, "title": ch_title, "image_urls": []})
        return results
    except Exception as e:
        print(f"    Vortex error: {e}")
        return []

# ─── Main ─────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("FALLBACK SYNC: Merging all sources into KV")
    print("=" * 60)
    
    # Load three-way mapping
    mapping = json.load(open("/home/hermes/manhwa-scraper/three_way_mapping.json"))
    matches = mapping["mapping"]
    print(f"Loaded {len(matches)} matched series")
    
    # For each matched series, fetch from all sources and merge
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                headers={"User-Agent": USER_AGENT}) as client:
        for i, m in enumerate(matches):
            canonical = m["canonical_title"]
            sources = m["sources"]
            print(f"\n[{i+1}/{len(matches)}] {canonical}")
            print(f"  Sources: {list(sources.keys())}")
            
            # Fetch from each source
            all_chapters = {}
            
            if "asura" in sources:
                slug = sources["asura"]["slug"]
                print(f"  Fetching Asura ({slug})...")
                chapters = await fetch_asura_chapters(client, slug)
                for ch in chapters:
                    num = ch["number"]
                    if num not in all_chapters:
                        all_chapters[num] = {"number": num, "title": ch["title"], "image_urls": []}
                    if ch["image_urls"]:
                        all_chapters[num]["image_urls"] = ch["image_urls"]
                print(f"    Got {len(chapters)} chapters, {sum(len(c['image_urls']) for c in chapters)} images")
            
            if "flame" in sources:
                flame_id = sources["flame"]["id"]
                print(f"  Fetching Flame (#{flame_id})...")
                chapters = await fetch_flame_chapters(client, flame_id)
                print(f"    Got {len(chapters)} chapters")
            
            if "vortex" in sources:
                vortex_id = sources["vortex"]["id"]
                print(f"  Fetching Vortex (#{vortex_id})...")
                chapters = await fetch_vortex_chapters(client, vortex_id)
                print(f"    Got {len(chapters)} chapters")
            
            # Build merged bundle
            merged_chapters = []
            for num in sorted(all_chapters.keys()):
                ch = all_chapters[num]
                merged_chapters.append({
                    "number": ch["number"],
                    "title": ch["title"],
                    "image_urls": ch["image_urls"],
                    "fallback_image_urls": [],  # TODO: populate from other sources
                })
            
            bundle = {
                "slug": sources.get("asura", {}).get("slug", ""),
                "title": canonical,
                "sources": {k: v.get("title", v.get("slug", "")) for k, v in sources.items()},
                "chapter_count": len(merged_chapters),
                "chapters": merged_chapters,
            }
            
            # Write to KV
            if bundle["slug"]:
                payload = {"series": bundle}
                req = urllib.request.Request(
                    f"{KV_BASE}/sync?token=dexmanhwa-sync-2026",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = json.loads(r.read().decode())
                print(f"  Synced: {result}")
            
            if (i + 1) % 10 == 0:
                print(f"\n  Progress: {i+1}/{len(matches)} series processed")
    
    print(f"\n{'='*60}")
    print("FALLBACK SYNC COMPLETE")
    print(f"{'='*60}")

if __name__ == "__main__":
    asyncio.run(main())
