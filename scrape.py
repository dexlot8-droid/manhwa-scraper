"""
Fetch chapter lists and page image URLs from webtoon sources.

Two sources are supported:

    AsuraSource     JSON API at api.asurascans.com
    HtmlSource      plain HTML pages (used by manhuaplus.org)

Each returns the same shapes, so you can swap one for the other:

    chapter_list(slug)   -> [{"number": 1, "title": "Chapter 1"}, ...]
    chapter_images(slug, n) -> ["https://.../page-1.jpg", ...]

Run it directly to see what comes back:

    python scrape.py --source asura --series nano-machine
"""

import asyncio
import re

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Pause between requests, per source, so we stay polite.
DELAY_DEFAULT = 0.3
DELAY_HTML = 0.6


class Source:
    """Base class. Subclasses set `name` and implement the two methods."""

    name = ""
    delay = DELAY_DEFAULT

    def __init__(self, client=None, **kwargs):
        # A client we were handed belongs to the caller: never close it.
        self._client = client
        self._owns_client = client is None

    async def _get(self, url, timeout=30):
        if self._client is None or self._client.is_closed:
            if self._owns_client is False and self._client is not None:
                # Caller's client died; build our own rather than reusing it.
                self._client = None
                self._owns_client = True
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            self._owns_client = True
        r = await self._client.get(url, timeout=timeout)
        if self.delay:
            await asyncio.sleep(self.delay)
        return r

    async def close(self):
        if self._client is not None and self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def chapter_list(self, slug):
        raise NotImplementedError

    async def chapter_images(self, slug, number):
        raise NotImplementedError

    async def series_info(self, slug):
        """Optional metadata. Returns {} when the source has none."""
        return {}


class AsuraSource(Source):
    """
    JSON API.

        GET /api/series/{slug}                -> metadata
        GET /api/series/{slug}/chapters       -> chapter list
        GET /api/series/{slug}/chapters/{n}   -> page URLs
    """

    name = "asura"
    base = "https://api.asurascans.com"
    delay = DELAY_DEFAULT

    async def series_info(self, slug):
        r = await self._get(f"{self.base}/api/series/{slug}")
        if r.status_code != 200:
            return {}
        return r.json().get("series", {})

    async def chapter_list(self, slug):
        r = await self._get(f"{self.base}/api/series/{slug}/chapters")
        if r.status_code != 200:
            return []
        out = []
        for c in r.json().get("data", []):
            number = c.get("number")
            if number is None:
                continue
            out.append({"number": float(number), "title": c.get("title") or f"Chapter {number:g}"})
        return out

    async def chapter_images(self, slug, number):
        r = await self._get(f"{self.base}/api/series/{slug}/chapters/{number:g}")
        if r.status_code != 200:
            return []
        chapter = r.json().get("data", {}).get("chapter", {})
        return [p["url"] for p in chapter.get("pages", []) if p.get("url")]


class HtmlSource(Source):
    """
    Plain HTML source with a JSON island for page URLs.

        GET /manga/{slug}                        -> chapter links
        GET /manga/{slug}/chapter-{n}            -> embeds a chapter id
        GET /ajax/image/list/chap/{chapter_id}   -> JSON holding page URLs

    Two requests per chapter, hence the longer delay.
    """

    name = "html"
    base = "https://manhuaplus.org"
    delay = DELAY_HTML

    def __init__(self, client=None, base=None, **kwargs):
        super().__init__(client=client, **kwargs)
        if base:
            self.base = base

    async def _get_retry(self, url, retries=3):
        for attempt in range(retries):
            try:
                r = await self._get(url, timeout=60)
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

    async def series_info(self, slug):
        r = await self._get_retry(f"{self.base}/manga/{slug}")
        if not r or r.status_code != 200:
            return {}
        html = r.text
        cover = re.search(
            r'(?:data-src|src)="(' + re.escape(self.base) + r"/uploads/covers/"
            + re.escape(slug) + r'\.[a-z]+)"',
            html,
        )
        desc = re.search(r'<meta name="description" content="([^"]{10,300})"', html)
        return {
            "cover": cover.group(1) if cover else f"{self.base}/uploads/covers/{slug}.jpg",
            "description": desc.group(1) if desc else None,
            "status": "ongoing",
        }

    async def chapter_list(self, slug):
        r = await self._get_retry(f"{self.base}/manga/{slug}")
        if not r or r.status_code != 200:
            return []
        pattern = (
            r'href="(' + re.escape(self.base) + r"/manga/" + re.escape(slug) + r"/chapter-([0-9.]+))\""
        )
        seen = {}
        for url, number in re.findall(pattern, r.text):
            try:
                n = float(number)
            except ValueError:
                continue
            seen.setdefault(n, {"number": n, "title": f"Chapter {n:g}"})
        return [seen[k] for k in sorted(seen)]

    async def chapter_images(self, slug, number):
        page = await self._get_retry(f"{self.base}/manga/{slug}/chapter-{number:g}")
        if not page or page.status_code != 200:
            return []
        m = re.search(r"const CHAPTER_ID = (\d+);", page.text)
        if not m:
            return []
        ajax = await self._get_retry(f"{self.base}/ajax/image/list/chap/{m.group(1)}")
        if not ajax or ajax.status_code != 200:
            return []
        try:
            data = ajax.json()
        except Exception:
            return []
        if not data.get("status"):
            return []
        urls = re.findall(r'src="(https://cdn\.manhuaplus\.cc/[^"]+)"', data.get("html", ""))
        return list(dict.fromkeys(urls))


SOURCES = {
    "asura": AsuraSource,
    "html": HtmlSource,
    "manhuaplus": HtmlSource,
}


def get_source(name="asura", client=None):
    cls = SOURCES.get((name or "").strip().lower())
    if cls is None:
        raise ValueError(f"unknown source {name!r}; try one of: {', '.join(SOURCES)}")
    return cls(client=client)


async def scrape_series(slug, source="asura", max_chapters=None, on_progress=None, client=None):
    """
    Fetch everything for one series.

    Returns:
        {
            "slug": str,
            "source": str,
            "info": {...},
            "chapters": [
                {"number": 1, "title": "Chapter 1", "images": ["https://..."]},
                ...
            ],
        }

    `max_chapters` limits how many chapters have their images fetched.
    `on_progress` is called as on_progress(done, total, chapter_number).
    Pass `client` to share one HTTP connection across several calls.
    """
    src = get_source(source, client=client)
    try:
        info = await src.series_info(slug)
        chapters = await src.chapter_list(slug)
        if not chapters:
            return {"slug": slug, "source": src.name, "info": info, "chapters": []}

        targets = chapters if max_chapters is None else chapters[:max_chapters]
        results = []
        for i, ch in enumerate(targets, start=1):
            images = await src.chapter_images(slug, ch["number"])
            results.append(
                {"number": ch["number"], "title": ch["title"], "images": images}
            )
            if on_progress:
                on_progress(i, len(targets), ch["number"])
        return {"slug": slug, "source": src.name, "info": info, "chapters": results}
    finally:
        await src.close()


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Scrape a webtoon series' chapters and image URLs.")
    ap.add_argument("--series", required=True, help="series slug on the source site")
    ap.add_argument("--source", default="asura", help="asura | html")
    ap.add_argument("--max-chapters", type=int, default=None, help="limit chapters fetched")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()

    def progress(done, total, number):
        print(f"  {done}/{total} chapter {number:g}", file=__import__("sys").stderr)

    data = asyncio.run(
        scrape_series(
            args.series,
            source=args.source,
            max_chapters=args.max_chapters,
            on_progress=progress,
        )
    )
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out}", file=__import__("sys").stderr)
    else:
        print(text)