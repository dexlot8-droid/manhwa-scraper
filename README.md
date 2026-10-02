# asura / html webtoon scraper

Fetches chapter lists and page image URLs from a webtoon source and hands them
back as plain Python data or JSON.

No database. No publishing. No cloud keys. One file.

## Install

```bash
pip install httpx
```

## Use it

From the command line:

```bash
python scrape.py --series nano-machine --source asura
python scrape.py --series some-title --source html --max-chapters 5 --out result.json
```

As a library:

```python
import asyncio
from scrape import scrape_series

data = asyncio.run(scrape_series("nano-machine", source="asura"))

print(data["info"].get("cover"))
for ch in data["chapters"]:
    print(ch["number"], len(ch["images"]), "pages")
```

Only want the chapter list, without fetching images? Use the source directly:

```python
import asyncio
from scrape import get_source

async def main():
    src = get_source("asura")
    chapters = await src.chapter_list("nano-machine")
    print(len(chapters), "chapters")
    images = await src.chapter_images("nano-machine", chapters[0]["number"])
    print(len(images), "pages in chapter 1")
    await src.close()

asyncio.run(main())
```

Or share one HTTP client across many series:

```python
import asyncio, httpx
from scrape import scrape_series

async def main():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for slug in ["series-a", "series-b"]:
            data = await scrape_series(slug, source="asura", client=client)
            print(slug, len(data["chapters"]))
```

## What you get back

```python
{
    "slug": "nano-machine",
    "source": "asura",
    "info": {"cover": "https://...", "description": "...", "status": "ongoing"},
    "chapters": [
        {"number": 1.0, "title": "Chapter 1", "images": ["https://...", "https://..."]},
    ],
}
```

## Sources

| name | site |
|---|---|
| `asura` | JSON API at `api.asurascans.com` |
| `html` | plain HTML pages (`manhuaplus.org`); override with `base=` |
| `manhuaplus` | alias for `html` |

## Adding a source

Subclass `Source` and implement two methods:

```python
from scrape import Source, SOURCES

class MySource(Source):
    name = "mysite"
    base = "https://example.org"

    async def chapter_list(self, slug):
        ...

    async def chapter_images(self, slug, number):
        ...

SOURCES["mysite"] = MySource
```

`series_info` is optional. Requests sleep automatically between calls; set
`delay` on your subclass to change the pace.

## Good citizenship

This fetches publicly available page metadata and image links. It does not
download images. Keep the delay reasonable, respect the site's terms, and do
not use the results in ways you have no licence for.

## License

MIT.