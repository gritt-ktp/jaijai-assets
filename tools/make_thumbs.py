"""Build small product thumbnails for the นายน้อย LINE bot.

Reads product/_sources.csv (code,url) — every produced item (Master Bom BOM No_),
url = its Pic link from Master FG Pic or blank. Downloads each photo, fits it on a
white 240x240 square canvas and saves product/<CODE>.jpg (~10-20 KB).
Existing thumbnails are skipped unless FORCE=1.
Failures are written to product/_failed.csv.
"""
import csv
import io
import os
import sys
import urllib3
from concurrent.futures import ThreadPoolExecutor

import requests
from urllib.parse import quote
from PIL import Image, ImageOps

W, H = 240, 240
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "product")
SRC = os.path.join(ROOT, "_sources.csv")
FAILED = os.path.join(ROOT, "_failed.csv")
FORCE = os.environ.get("FORCE") == "1"
LIMIT = int(os.environ.get("LIMIT") or 0)          # >0 = only first N rows (test runs)
CODES = {c.strip().upper() for c in os.environ.get("CODES", "").split(",") if c.strip()}
TIMEOUT = (8, 20)                                  # connect, read seconds
PROXY = "https://images.weserv.nl/?url="          # fallback when gammaco.com is slow/blocked
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
session = requests.Session()
session.headers["User-Agent"] = UA


def _get(url, **kw):
    r = session.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image"):
        raise ValueError(f"not an image ({r.headers.get('content-type')})")
    return r.content


def fetch(url):
    errs = []
    try:
        return _get(url)
    except requests.exceptions.SSLError as e:
        errs.append(f"direct SSL: {e}")
        # gammaco.com may serve an incomplete certificate chain; these are public photos
        try:
            return _get(url, verify=False)
        except Exception as e2:  # noqa: BLE001
            errs.append(f"direct no-verify: {type(e2).__name__}: {e2}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"direct: {type(e).__name__}: {e}")
    # direct download failed or timed out → same photo through the image proxy
    try:
        return _get(PROXY + quote(url, safe=""))
    except Exception as e:  # noqa: BLE001
        errs.append(f"proxy: {type(e).__name__}: {e}")
    raise RuntimeError(" | ".join(errs))


ICON = os.path.join(ROOT, "..", "package.png")
PREV_FAILED = set()
if os.path.exists(FAILED):
    with open(FAILED, newline="", encoding="utf-8") as f:
        PREV_FAILED = {r["code"].strip().upper() for r in csv.DictReader(f)}


def placeholder(out):
    """Light-grey square with the package icon — shown when the website has no photo."""
    canvas = Image.new("RGB", (W, H), (241, 241, 241))
    try:
        icon = Image.open(ICON).convert("RGBA")
        icon.thumbnail((W // 3, H // 3), Image.LANCZOS)
        canvas.paste(icon, ((W - icon.width) // 2, (H - icon.height) // 2), icon)
    except Exception:  # noqa: BLE001
        pass
    canvas.save(out, "JPEG", quality=80, optimize=True)


def make(code, url):
    out = os.path.join(ROOT, f"{code}.jpg")
    # skip finished thumbnails; codes that were placeholders last time are retried
    if os.path.exists(out) and not FORCE and code not in PREV_FAILED:
        return code, "skip", ""
    if not url:
        placeholder(out)           # item has no photo in Master FG Pic → grey placeholder
        return code, "fail", "no photo link in Master FG Pic"
    try:
        img = Image.open(io.BytesIO(fetch(url)))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img = ImageOps.contain(img, (W, H), Image.LANCZOS)
        canvas = Image.new("RGB", (W, H), "white")
        canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
        canvas.save(out, "JPEG", quality=80, optimize=True, progressive=True)
        return code, "ok", ""
    except Exception as e:  # noqa: BLE001
        placeholder(out)           # never leave the bot with a missing file
        return code, "fail", f"{type(e).__name__}: {e}"


def main():
    with open(SRC, newline="", encoding="utf-8") as f:
        rows = [(r["code"].strip().upper(), r["url"].strip()) for r in csv.DictReader(f)]
    if CODES:
        rows = [r for r in rows if r[0] in CODES]
    if LIMIT:
        rows = rows[:LIMIT]
    done = [0]

    def work(r):
        res = make(*r)
        done[0] += 1
        if done[0] % 50 == 0 or res[1] == "fail":
            print(f"[{done[0]}/{len(rows)}] {res[0]} {res[1]} {res[2]}", flush=True)
        return res
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, rows))
    fails = [(c, e) for c, s, e in results if s == "fail"]
    ok = sum(1 for _, s, _ in results if s == "ok")
    skip = sum(1 for _, s, _ in results if s == "skip")
    with open(FAILED, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "error"])
        w.writerows(fails)
    print(f"made {ok}, skipped {skip}, failed {len(fails)} of {len(rows)}")
    for c, e in fails[:20]:
        print("  FAIL", c, e)
    # GitHub annotations: visible on the run page (and via the API) without opening logs
    for c, e in fails[:5]:
        print(f"::warning title=thumb {c}::{e[:300]}")
    print(f"::notice title=thumbs::made {ok}, skipped {skip}, failed {len(fails)} of {len(rows)}")
    # GitHub annotations: visible on the run page (and via the API) without opening logs
    for c, e in fails[:5]:
        print(f"::warning title=thumb {c}::{e[:300]}")
    print(f"::notice title=thumbs::made {ok}, skipped {skip}, failed {len(fails)} of {len(rows)}")
    # don't fail the job for a few broken photos; fail only if nothing worked
    if rows and ok + skip == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
