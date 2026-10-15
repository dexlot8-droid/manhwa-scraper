#!/usr/bin/env python3
"""
Fallback sync v2: 1 fallback per series, fixed endpoints.

For each matched series:
1. Pick primary source (Asura > Flame > Vortex > Mgeko)
2. Pick 1 fallback source (next available)
3. Fetch chapters from primary + fallback
4. Merge into single bundle with fallback_image_urls
5. Write to KV
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

# Source priority: Asura (best quality) > Vortex > Flame > Mgeko
SOURCE_PRIORITY = ["asura", "vortex", "flame", "mgeko"]

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

async def fetch_vortex_chapters(client, vortex_id):
    """Fetch all chapters with images from Vortex."""
    try:
        # Get chapter list
        r = await _mp_get(client, f"{VORTEX_API}/api/chapters?postId={vortex_id}&limit=1000")
        if not r or r.status_code != 200:
            return []
        data = r.json()
        post = data.get("post", {})
        chapters = post.get("chapters", [])
        
        results = []
        for ch in chapters:
            ch_id = ch.get("id")
            ch_num = ch.get("number")
            ch_title = ch.get("title") or f"Chapter {ch_num:g}"
            
            # Fetch chapter detail for images
            r2 = await _mp_get(client, f"{VORTEX_API}/api/chapter?chapterId={ch_id}")
            if not r2 or r2.status_code != 200:
                results.append({"number": ch_num, "title": ch_title, "image_urls": []})
                continue
            ch_data = r2.json().get("chapter", {})
            images = [img["url"] for img in ch_data.get("images", []) if img.get("url")]
            created_at = ch_data.get("createdAt", "")
            results.append({"number": ch_num, "title": ch_title, "image_urls": images, "added_at": created_at})
        return results
    except Exception as e:
        print(f"    Vortex error: {e}")
        return []

async def fetch_flame_chapters(client, flame_id):
    """Fetch all chapters with images from Flame."""
    try:
        # Get chapter list from series page __NEXT_DATA__
        r = await _mp_get(client, f"{FLAME_API}/series/{flame_id}")
        if not r or r.status_code != 200:
            return []
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', r.text)
        if not m:
            return []
        data = json.loads(m.group(1))
        
        def find_chapters(obj, depth=0):
            if depth > 15: return None
            if isinstance(obj, list) and len(obj) > 0:
                if isinstance(obj[0], dict) and 'chapter_id' in obj[0]:
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
            ch_id = ch.get("chapter_id")
            ch_num = float(ch.get("chapter", 0))
            token = ch.get("token", "")
            ch_title = ch.get("title") or f"Chapter {ch_num:g}"
            
            # Fetch chapter page for images
            r2 = await _mp_get(client, f"{FLAME_API}/series/{flame_id}/{token}")
            if not r2 or r2.status_code != 200:
                results.append({"number": ch_num, "title": ch_title, "image_urls": []})
                continue
            # Extract image URLs from HTML directly
            urls = re.findall(r'https://cdn\.flamecomics\.xyz/uploads/images/series/' + str(flame_id) + r'/' + re.escape(token) + r'/[^"\s]+\.(?:jpg|webp|png)', r2.text)
            # Filter out thumbnails and other non-chapter images
            images = [u for u in urls if 'thumbnail' not in u and 'read_on' not in u and 'logo' not in u]
            # release_date is unix timestamp
            release_date = ch.get("release_date", 0)
            added_at = ""
            if release_date:
                from datetime import datetime, timezone
                added_at = datetime.fromtimestamp(release_date, tz=timezone.utc).isoformat()
            results.append({"number": ch_num, "title": ch_title, "image_urls": images, "added_at": added_at})
        return results
    except Exception as e:
        print(f"    Flame error: {e}")
        return []

# ─── Main ─────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("FALLBACK SYNC v2: 1 fallback per series")
    print("=" * 60)
    
    # Load four-way mapping
    mapping = json.load(open("/home/hermes/manhwa-scraper/four_way_mapping.json"))
    matches = mapping["matches"]
    print(f"Loaded {len(matches)} matched series")
    
    # Filter: only series with 2+ sources
    multi_source = [m for m in matches if len(m.get("sources", {})) >= 2]
    print(f"Multi-source series: {len(multi_source)}")
    
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                headers={"User-Agent": USER_AGENT}) as client:
        for i, m in enumerate(multi_source):
            canonical = m["canonical_title"]
            sources = m["sources"]
            
            # Pick primary and fallback based on priority
            primary_source = None
            fallback_source = None
            for src in SOURCE_PRIORITY:
                if src in sources:
                    if primary_source is None:
                        primary_source = src
                    elif fallback_source is None:
                        fallback_source = src
                        break
            
            if not primary_source or not fallback_source:
                continue
            
            print(f"\n[{i+1}/{len(multi_source)}] {canonical}")
            print(f"  Primary: {primary_source}, Fallback: {fallback_source}")
            
            # Fetch from primary
            primary_chapters = []
            if primary_source == "asura":
                slug = sources["asura"]["slug"]
                print(f"  Fetching Asura ({slug})...")
                primary_chapters = await fetch_asura_chapters(client, slug)
            elif primary_source == "vortex":
                vid = sources["vortex"]["id"]
                print(f"  Fetching Vortex (#{vid})...")
                primary_chapters = await fetch_vortex_chapters(client, vid)
            elif primary_source == "flame":
                fid = sources["flame"]["id"]
                print(f"  Fetching Flame (#{fid})...")
                primary_chapters = await fetch_flame_chapters(client, fid)
            
            print(f"    Got {len(primary_chapters)} chapters, {sum(len(c['image_urls']) for c in primary_chapters)} images")
            
            # Fetch from fallback
            fallback_chapters = []
            if fallback_source == "asura":
                slug = sources["asura"]["slug"]
                print(f"  Fetching fallback Asura ({slug})...")
                fallback_chapters = await fetch_asura_chapters(client, slug)
            elif fallback_source == "vortex":
                vid = sources["vortex"]["id"]
                print(f"  Fetching fallback Vortex (#{vid})...")
                fallback_chapters = await fetch_vortex_chapters(client, vid)
            elif fallback_source == "flame":
                fid = sources["flame"]["id"]
                print(f"  Fetching fallback Flame (#{fid})...")
                fallback_chapters = await fetch_flame_chapters(client, fid)
            
            print(f"    Got {len(fallback_chapters)} chapters, {sum(len(c['image_urls']) for c in fallback_chapters)} images")
            
            # Merge: primary images + fallback images for missing chapters
            fallback_by_num = {c["number"]: c for c in fallback_chapters}
            merged_chapters = []
            for ch in primary_chapters:
                fb = fallback_by_num.get(ch["number"], {})
                merged_chapters.append({
                    "number": ch["number"],
                    "title": ch["title"],
                    "image_urls": ch["image_urls"],
                    "fallback_image_urls": fb.get("image_urls", []),
                    "added_at": ch.get("added_at", fb.get("added_at", "")),
                })
            
            # Build bundle
            bundle = {
                "slug": sources.get("asura", {}).get("slug", sources.get("vortex", {}).get("slug", "")),
                "title": canonical,
                "primary_source": primary_source,
                "fallback_source": fallback_source,
                "sources": {k: v.get("title", v.get("slug", "")) for k, v in sources.items()},
                "chapter_count": len(merged_chapters),
                "chapters": merged_chapters,
            }
            
            # Write to KV
            if bundle["slug"]:
                payload = {"series": bundle}
                req = urllib.request.Request(
                    f"{KV_BASE}/sync",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = json.loads(r.read().decode())
                print(f"  Synced: {result}")
            
            if (i + 1) % 10 == 0:
                print(f"\n  Progress: {i+1}/{len(multi_source)} series processed")
    
    print(f"\n{'='*60}")
    print("FALLBACK SYNC v2 COMPLETE")
    print(f"{'='*60}")

if __name__ == "__main__":
    asyncio.run(main())
