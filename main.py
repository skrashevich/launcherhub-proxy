"""
LauncherHub-compatible API proxy for mrekin.duckdns.org flasher.

Aggregates firmware from all available repositories and automatically finds
related device variants using family-based name matching.

Family matching algorithm:
  1. Find the "family root" – the longest dash-separated prefix of the
     requested device name that is a known firmware device.
     E.g. "t-deck-plus" → root "t-deck"  (if "t-deck-plus" is unknown but "t-deck" is)
  2. Collect all known devices sharing that root as a prefix.
     E.g. "t-deck" → {t-deck, t-deck-tft, t-deck-tft-ru, t-deck-plus, t-deck-ru, …}

Endpoints:
  GET /firmwares?category=<OTA_TAG>[&order_by=...][&page=N][&q=...][&star=1]
  GET /firmwares?fid=<fid>
  GET /download?fid=<fid>&file=<url>

fid format: "<device>|<version>|<src>"
  - src is optional for backwards compatibility (defaults to "Official repo")
  - e.g. "t-deck|v2.7.18.fb3bf78|Official repo"
  - e.g. "t-deck-tft|v2.7.19.bb3d6d5|svk"

Run:
  pip install fastapi uvicorn httpx
  uvicorn main:app --host 0.0.0.0 --port 8000

For HTTPS (required by the firmware, which uses WiFiClientSecure):
  uvicorn main:app --host 0.0.0.0 --port 443 \
      --ssl-keyfile key.pem --ssl-certfile cert.pem
"""

import asyncio
import os
import struct
import time
import logging
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UPSTREAM_BASE = "https://mrekin.duckdns.org/flasher/api"
DEFAULT_SRC   = "Official repo"

# How many firmware entries per page
PAGE_SIZE = 10

# Partition table sits at 0x8000 in the flash image; we read 0x1A0 bytes (13 entries × 32 bytes)
PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE   = 0x1B0   # a bit extra, safe margin

log = logging.getLogger("launcherhub")
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="LauncherHub-compatible proxy")


@app.on_event("startup")
async def _on_startup():
    log.info("LauncherHub proxy started — UPSTREAM=%s LOG_LEVEL=%s", UPSTREAM_BASE, _LOG_LEVEL)

# In-memory caches
_sources_cache: list[dict] | None = None
_all_devices_cache: set[str] | None = None


# ---------------------------------------------------------------------------
# Partition-table parser (mirrors the C++ logic in installExtFirmware)
# ---------------------------------------------------------------------------

def parse_partition_table(data: bytes) -> dict:
    """
    Parse an ESP32 partition table blob (read from offset 0x8000 of the .bin).
    Returns a dict with the fields expected by the Launcher firmware JSON.
    """
    result = {
        "nb": True,          # no-bootloader flag: True  → file is app-only (no PT)
        "s":  False,         # has SPIFFS/LittleFS
        "f":  False,         # has FAT partition 1
        "f2": False,         # has FAT partition 2
        "ao": 0x10000,       # app offset  (default)
        "as": 0,             # app size
        "so": 0,             # spiffs offset
        "ss": 0,             # spiffs size
        "fo": 0,             # fat1 offset
        "fs": 0,             # fat1 size
        "fo2": 0,            # fat2 offset
        "fs2": 0,            # fat2 size
    }

    # First byte of a valid partition table magic is 0xAA
    if not data or data[0] != 0xAA:
        log.debug("No valid partition table (first byte=0x%02x, data_len=%d) — treating as nb=True",
                  data[0] if data else 0xFF, len(data))
        return result

    result["nb"] = False
    fat_count = 0

    for i in range(0, min(len(data), 0x1A0 + 0x20), 0x20):
        entry = data[i:i + 0x20]
        if len(entry) < 16:
            break
        # Partition magic
        if entry[0] != 0xAA or entry[1] != 0x50:
            continue

        ptype    = entry[2]
        subtype  = entry[3]

        # Offset is stored little-endian in bytes 4-7 (32-bit)
        offset = struct.unpack_from("<I", entry, 4)[0]
        # Size   is stored little-endian in bytes 8-11
        size   = struct.unpack_from("<I", entry, 8)[0]

        # App / OTA (type=0x00, subtype 0x00..0x1F)
        if ptype == 0x00 and (subtype == 0x00 or 0x10 <= subtype <= 0x1F):
            if result["as"] == 0:          # first app partition wins
                result["ao"] = offset
                result["as"] = size
            log.debug("  APP  partition: type=0x%02x sub=0x%02x offset=0x%x size=0x%x (%d KiB)",
                      ptype, subtype, offset, size, size // 1024)

        # SPIFFS / LittleFS (type=0x01, subtype=0x82 or 0x83)
        elif ptype == 0x01 and subtype in (0x82, 0x83):
            result["s"]  = True
            result["so"] = offset
            result["ss"] = size
            log.debug("  SPIFFS partition: type=0x%02x sub=0x%02x offset=0x%x size=0x%x (%d KiB)",
                      ptype, subtype, offset, size, size // 1024)

        # FAT (type=0x01, subtype=0x81)
        elif ptype == 0x01 and subtype == 0x81:
            if fat_count == 0:
                result["f"]  = True
                result["fo"] = offset
                result["fs"] = size
                log.debug("  FAT1 partition: type=0x%02x sub=0x%02x offset=0x%x size=0x%x (%d KiB)",
                          ptype, subtype, offset, size, size // 1024)
            elif fat_count == 1:
                result["f2"]  = True
                result["fo2"] = offset
                result["fs2"] = size
                log.debug("  FAT2 partition: type=0x%02x sub=0x%02x offset=0x%x size=0x%x (%d KiB)",
                          ptype, subtype, offset, size, size // 1024)
            fat_count += 1
        else:
            log.debug("  SKIP partition: type=0x%02x sub=0x%02x offset=0x%x size=0x%x",
                      ptype, subtype, offset, size)

    log.debug("Partition table result: %s", result)
    return result


async def fetch_partition_info(fw_url: str) -> tuple[dict, int]:
    """
    Fetch the partition-table region of the firmware binary and parse it.

    Returns (partition_dict, file_size_bytes).
    file_size comes from the Content-Length header of the 200 response
    (0 when unknown or when a 206 partial response is used).

    Strategy:
    1. Send a Range request for bytes 0x8000..0x8000+0x1B0.
    2. If the server honours it (206 Partial Content) – parse directly.
    3. If the server ignores Range (200 OK) – stream the response, capture
       Content-Length, skip the first 0x8000 bytes, read 0x1B0 bytes, stop.
    Falls back to (nb=True defaults, 0) on any error.
    """
    byte_start = PARTITION_TABLE_OFFSET
    byte_end   = PARTITION_TABLE_OFFSET + PARTITION_TABLE_SIZE - 1

    log.debug("fetch_partition_info: GET %s Range=bytes=%d-%d", fw_url, byte_start, byte_end)
    t0 = time.monotonic()

    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=30) as client:
            async with client.stream(
                "GET", fw_url, headers={"Range": f"bytes={byte_start}-{byte_end}"}
            ) as r:
                elapsed_headers = time.monotonic() - t0
                log.debug("fetch_partition_info: HTTP %d (headers in %.2fs) url=%s",
                          r.status_code, elapsed_headers, fw_url)
                log.debug("fetch_partition_info: response headers: %s",
                          dict(r.headers))

                if r.status_code == 206:
                    # Extract total file size from Content-Range header, e.g.
                    # "Content-Range: bytes 32768-33199/3582768"
                    cr = r.headers.get("content-range", "")
                    try:
                        file_size = int(cr.split("/")[-1])
                    except (ValueError, IndexError):
                        file_size = 0
                    data = await r.aread()
                    elapsed = time.monotonic() - t0
                    log.info("Partition fetch (206 Range): %d bytes read, file_size=%d in %.2fs — %s",
                             len(data), file_size, elapsed, fw_url)
                    return parse_partition_table(data), file_size

                if r.status_code == 200:
                    file_size = int(r.headers.get("content-length", 0))
                    log.info("Server ignored Range (%s) — streaming to 0x%x (content-length=%d)",
                             fw_url, byte_start, file_size)
                    collected = bytearray()
                    skipped = 0
                    chunks_received = 0
                    async for chunk in r.aiter_bytes(chunk_size=8192):
                        chunks_received += 1
                        if skipped < byte_start:
                            need = byte_start - skipped
                            if len(chunk) <= need:
                                skipped += len(chunk)
                                continue
                            chunk = chunk[need:]
                            skipped = byte_start
                        collected.extend(chunk)
                        if len(collected) >= PARTITION_TABLE_SIZE:
                            break
                    data = bytes(collected[:PARTITION_TABLE_SIZE])
                    elapsed = time.monotonic() - t0
                    log.info("Partition fetch (200 stream): %d PT bytes from %d chunks in %.2fs — %s",
                             len(data), chunks_received, elapsed, fw_url)
                    return parse_partition_table(data), file_size

                log.warning("Unexpected HTTP %d from %s", r.status_code, fw_url)
                return parse_partition_table(b""), 0

    except Exception as exc:
        elapsed = time.monotonic() - t0
        log.warning("fetch_partition_info failed after %.2fs for %s: %s", elapsed, fw_url, exc)
        return parse_partition_table(b""), 0


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

async def get_all_sources() -> list[dict]:
    """Fetch available firmware sources from upstream (cached in memory)."""
    global _sources_cache
    if _sources_cache is not None:
        log.debug("get_all_sources: cache hit (%d sources)", len(_sources_cache))
        return _sources_cache
    log.debug("get_all_sources: cache miss — fetching %s/srcs", UPSTREAM_BASE)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=15) as client:
            r = await client.get(f"{UPSTREAM_BASE}/srcs")
            r.raise_for_status()
            _sources_cache = r.json()
            log.info("Loaded %d firmware sources in %.2fs: %s",
                     len(_sources_cache), time.monotonic() - t0,
                     [s["src"] for s in _sources_cache])
            log.debug("Sources detail: %s", _sources_cache)
    except Exception as exc:
        log.warning("Failed to fetch sources list (%.2fs): %s — falling back to default",
                    time.monotonic() - t0, exc)
        _sources_cache = [{"src": DEFAULT_SRC, "desc": "Official Meshtastic firmware", "type": "meshtastic"}]
    return _sources_cache


async def get_all_devices() -> set[str]:
    """
    Fetch all known device names from every source (cached in memory).

    The no-src /availableFirmwares endpoint only returns devices from the
    official repository.  To also discover devices that exist exclusively in
    third-party sources (e.g. t-deck-tft-ru in svk), we query each source
    individually and union the results.
    """
    global _all_devices_cache
    if _all_devices_cache is not None:
        log.debug("get_all_devices: cache hit (%d devices)", len(_all_devices_cache))
        return _all_devices_cache

    sources = await get_all_sources()
    log.debug("get_all_devices: cache miss — querying %d sources", len(sources))
    t0 = time.monotonic()

    async def _fetch_for_src(src: str) -> set[str]:
        log.debug("get_all_devices: fetching device list for src=%r", src)
        try:
            async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=15) as client:
                r = await client.get(f"{UPSTREAM_BASE}/availableFirmwares", params={"src": src})
                r.raise_for_status()
                data = r.json()
            found: set[str] = set()
            for key in ("espdevices", "uf2devices", "rp2040devices"):
                bucket = data.get(key, [])
                log.debug("  src=%r key=%s devices=%d: %s", src, key, len(bucket), sorted(bucket))
                found.update(bucket)
            log.debug("get_all_devices: src=%r total=%d", src, len(found))
            return found
        except Exception as exc:
            log.warning("Failed to fetch device list for src=%r: %s", src, exc)
            return set()

    per_source = await asyncio.gather(*[_fetch_for_src(s["src"]) for s in sources])
    devices: set[str] = set()
    for s in per_source:
        devices |= s

    _all_devices_cache = devices
    log.info("Loaded %d unique device names across %d sources in %.2fs",
             len(devices), len(sources), time.monotonic() - t0)
    log.debug("All known devices: %s", sorted(devices))
    return _all_devices_cache


# ---------------------------------------------------------------------------
# Device family matching
# ---------------------------------------------------------------------------

def _find_family_root(category: str, all_devices: set[str]) -> str:
    """
    Find the longest dash-separated prefix of `category` that is a known device name.

    Examples (given all_devices contains "t-deck" but not "t-deck-plus"):
      "t-deck"      → "t-deck"   (exact match)
      "t-deck-plus" → "t-deck"   (longest known prefix)
      "unknown-xyz" → "unknown-xyz" (no match – returned as-is)
    """
    if category in all_devices:
        log.debug("family_root: exact match %r", category)
        return category
    parts = category.split("-")
    for n in range(len(parts) - 1, 0, -1):
        prefix = "-".join(parts[:n])
        log.debug("family_root: trying prefix %r", prefix)
        if prefix in all_devices:
            log.debug("family_root: %r → root %r", category, prefix)
            return prefix
    log.debug("family_root: no known prefix for %r — using as-is", category)
    return category


def find_related_devices(category: str, all_devices: set[str]) -> list[str]:
    """
    Return all firmware device names that belong to the same family as `category`.

    The family is anchored at the family root and includes every known device
    whose name starts with "<root>-" or "<root>_".

    The requested `category` is always first in the result (even if unknown),
    followed by family members in alphabetical order.
    """
    root = _find_family_root(category, all_devices)
    related: set[str] = {category}
    for device in all_devices:
        if device == root or device.startswith(root + "-") or device.startswith(root + "_"):
            related.add(device)
    result = [category] + sorted(d for d in related if d != category)
    log.debug("find_related_devices: %r → root=%r → %s", category, root, result)
    return result


# ---------------------------------------------------------------------------
# Upstream helpers
# ---------------------------------------------------------------------------

async def upstream_versions(device: str, src: str = DEFAULT_SRC) -> dict:
    url = f"{UPSTREAM_BASE}/versions"
    log.debug("upstream_versions: GET %s t=%r src=%r", url, device, src)
    t0 = time.monotonic()
    async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=15) as client:
        r = await client.get(url, params={"t": device, "src": src})
        r.raise_for_status()
        data = r.json()
    versions = data.get("versions", [])
    log.debug("upstream_versions: t=%r src=%r → %d versions in %.2fs: %s",
              device, src, len(versions), time.monotonic() - t0, versions)
    return data


def firmware_url(device: str, version: str, src: str = DEFAULT_SRC) -> str:
    # u=1 → raw OTA binary (.bin), matching the official api.launcherhub.net format
    # where `file` is always a direct binary link, not a ZIP archive.
    return (
        f"{UPSTREAM_BASE}/firmware"
        f"?t={device}&v={version}&u=1&p=fw&e=true&src={quote(src)}"
    )


def firmware_zip_url(device: str, version: str, src: str = DEFAULT_SRC) -> str:
    # u=5 → ZIP with bootloader, partitions, factory binary and littlefs.
    # Used for the `zip_url` field (Download to SD).
    return (
        f"{UPSTREAM_BASE}/firmware"
        f"?t={device}&v={version}&u=5&p=fw&e=true&src={quote(src)}"
    )



def make_fid(device: str, version: str, src: str) -> str:
    return f"{device}|{version}|{src}"


def parse_fid(fid: str) -> tuple[str, str, str]:
    """Parse fid into (device, version, src). src defaults to DEFAULT_SRC."""
    parts = fid.split("|", 2)
    if len(parts) < 2:
        raise ValueError(f"Invalid fid format: {fid!r}")
    device  = parts[0]
    version = parts[1]
    src     = parts[2] if len(parts) > 2 else DEFAULT_SRC
    return device, version, src


# ---------------------------------------------------------------------------
# Multi-source version fetching
# ---------------------------------------------------------------------------

async def _fetch_all_versions(
    device_variants: list[str],
    sources: list[dict],
) -> list[tuple[str, str, dict]]:
    """
    Fetch versions for all (device, source) combinations concurrently.
    Returns list of (device, src, versions_data) for combinations that have results.
    """
    combinations = [
        (device, source["src"])
        for device in device_variants
        for source in sources
    ]
    log.debug("_fetch_all_versions: %d combinations (%s × %s)",
              len(combinations), device_variants, [s["src"] for s in sources])
    t0 = time.monotonic()

    async def fetch_one(device: str, src: str):
        try:
            data = await upstream_versions(device, src)
            n = len(data.get("versions", []))
            log.debug("  fetch_one: device=%r src=%r → %d versions", device, src, n)
            return (device, src, data)
        except Exception as exc:
            log.debug("  fetch_one: device=%r src=%r → no data (%s)", device, src, exc)
            return None

    results = await asyncio.gather(*[fetch_one(d, s) for d, s in combinations])
    found = [r for r in results if r is not None and r[2].get("versions")]
    log.debug("_fetch_all_versions: %d/%d combinations have results in %.2fs",
              len(found), len(combinations), time.monotonic() - t0)
    return found


# ---------------------------------------------------------------------------
# /firmwares
# ---------------------------------------------------------------------------

@app.get("/firmwares")
async def get_firmwares(
    category:  Optional[str] = Query(None),
    fid:       Optional[str] = Query(None),
    order_by:  str           = Query("downloads"),
    page:      int           = Query(1, ge=1),
    q:         Optional[str] = Query(None),
    star:      Optional[int] = Query(None),
):
    log.debug("GET /firmwares fid=%r category=%r order_by=%r page=%d q=%r star=%r",
              fid, category, order_by, page, q, star)

    # ---- Mode 1: detail for a single firmware ----
    if fid:
        return await _get_version_detail(fid)

    # ---- Mode 2: firmware list ----
    if not category:
        raise HTTPException(status_code=400, detail="'category' query param is required")

    # Determine which device variants to query via automatic family matching
    all_devices, sources = await asyncio.gather(get_all_devices(), get_all_sources())
    device_variants = find_related_devices(category, all_devices)
    log.info("Family for %r: %s", category, device_variants)
    all_results = await _fetch_all_versions(device_variants, sources)

    # Build unified firmware item list
    items: list[dict] = []
    for device, src, data in all_results:
        versions_list: list[str] = data.get("versions", [])
        dates: dict = data.get("dates", {})

        for v in versions_list:
            # Display name: include device name and source
            name = f"{device} {v} [{src}]"
            items.append({
                "fid":    make_fid(device, v, src),
                "name":   name,
                "author": src,
                "star":   False,
                "_date":  dates.get(v, ""),
            })

    # --- Sorting ---
    if order_by == "name":
        items.sort(key=lambda x: x["name"])
    else:
        # Default ("downloads") and "date" both sort newest-first
        items.sort(key=lambda x: x["_date"], reverse=True)

    # --- Text search ---
    if q:
        ql = q.lower()
        items = [i for i in items if ql in i["name"].lower() or ql in i["author"].lower()]

    # --- Starred filter ---
    if star == 1:
        items = [i for i in items if i["star"]]

    # --- Pagination ---
    total = len(items)
    start = (page - 1) * PAGE_SIZE
    page_items = items[start: start + PAGE_SIZE]
    log.debug("GET /firmwares category=%r: total=%d page=%d/%d items_on_page=%d",
              category, total, page, -(-total // PAGE_SIZE) or 1, len(page_items))

    result_items = [
        {"fid": i["fid"], "name": i["name"], "author": i["author"], "star": i["star"]}
        for i in page_items
    ]

    return {
        "total":     total,
        "page_size": PAGE_SIZE,
        "page":      page,
        "items":     result_items,
    }


async def _get_version_detail(fid: str) -> dict:
    """
    fid format: "<device>|<version>|<src>"
    (src is optional for backwards compatibility – defaults to DEFAULT_SRC)

    Returns the full firmware object with `versions` array expected by loopVersions().
    """
    if "|" not in fid:
        raise HTTPException(
            status_code=400,
            detail="fid must be in format 'device|version[|src]', e.g. 't-deck|v2.7.18.fb3bf78|Official repo'"
        )

    try:
        device, version, src = parse_fid(fid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        upstream = await upstream_versions(device, src)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    dates = upstream.get("dates", {})
    all_versions: list[str] = upstream.get("versions", [])

    if version not in all_versions:
        raise HTTPException(status_code=404, detail=f"Version '{version}' not found for device '{device}' in source '{src}'")

    fw_url  = firmware_url(device, version, src)
    zip_url = firmware_zip_url(device, version, src)
    log.info("Fetching partition info for %s %s [%s]", device, version, src)
    log.debug("  fw_url=%s", fw_url)
    log.debug("  zip_url=%s", zip_url)

    t0 = time.monotonic()
    pt, file_size = await fetch_partition_info(fw_url)
    log.debug("fetch_partition_info done in %.2fs: pt=%s file_size=%d",
              time.monotonic() - t0, pt, file_size)

    # When nb=True the file IS the app binary: use the file size for `as` / `Fs`.
    # When nb=False we have proper partition offsets; estimate Fs as ao+as when
    # the server didn't return full file size via Content-Range.
    app_size = pt["as"] if pt["as"] else file_size
    if not file_size:
        file_size = (pt["ao"] + pt["as"]) if pt["as"] else 0
    log.debug("_get_version_detail: app_size=%d file_size=%d nb=%s s=%s f=%s f2=%s",
              app_size, file_size, pt["nb"], pt["s"], pt["f"], pt["f2"])

    version_obj = {
        "version":      version,
        "published_at": dates.get(version, ""),
        "file":         fw_url,
        "zip_url":      zip_url,
        # File size (matches official LauncherHub `Fs` field)
        "Fs": file_size,
        # Partition fields
        "nb": pt["nb"],
        "s":  pt["s"],
        "f":  pt["f"],
        "f2": pt["f2"],
        "as": app_size,
        "ao": pt["ao"],
        "ss": pt["ss"],
        "so": pt["so"],
        "fs": pt["fs"],
        "fo": pt["fo"],
        "fs2": pt["fs2"],
        "fo2": pt["fo2"],
    }

    return {
        "fid":    fid,
        "name":   f"{device} {version} [{src}]",
        "author": src,
        "star":   False,
        "versions": [version_obj],
    }


# ---------------------------------------------------------------------------
# /download  – proxy / redirect to the actual binary
# ---------------------------------------------------------------------------

@app.get("/download")
async def download_firmware(
    fid:  Optional[str] = Query(None),
    file: Optional[str] = Query(None),
):
    """
    The Launcher firmware calls:
        https://<host>/download?fid=<device>|<version>|<src>&file=<fw_url>

    We just stream the binary from upstream (following redirects).
    'file' already contains the full upstream URL built by _get_version_detail,
    so we proxy it transparently.
    """
    log.debug("GET /download fid=%r file=%r", fid, file)

    # Always prefer to reconstruct the URL from `fid`.
    # The `file` parameter is sent by the Launcher firmware without URL-encoding
    # its `&` separators, so FastAPI sees only the fragment up to the first `&`
    # (e.g. "https://…/firmware?t=t-deck-tft-ru" instead of the full URL).
    # Reconstructing from `fid` is safe and always gives the correct URL.
    target_url = None
    if fid and "|" in fid:
        try:
            device, version, src = parse_fid(fid)
            target_url = firmware_url(device, version, src)
            log.debug("/download: URL reconstructed from fid=%r → %s", fid, target_url)
        except ValueError as exc:
            log.warning("/download: failed to parse fid=%r: %s", fid, exc)

    # Last resort: use whatever the `file` param contained (may be truncated)
    if not target_url:
        target_url = file
        log.debug("/download: using raw `file` param (may be truncated): %s", target_url)

    if not target_url:
        raise HTTPException(status_code=400, detail="Provide 'file' or a valid 'fid' parameter")

    log.info("Proxying firmware download: %s", target_url)

    # Open the upstream connection eagerly so we can read Content-Length from
    # the actual GET response headers before we start yielding to the client.
    # A separate HEAD request would risk a mismatch (different redirect path,
    # different encoding) that causes "Response content longer than Content-Length".
    client = httpx.AsyncClient(verify=False, follow_redirects=True, timeout=120)
    upstream = await client.send(client.build_request("GET", target_url), stream=True)
    try:
        upstream.raise_for_status()
    except Exception:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Upstream firmware fetch failed")

    content_length = upstream.headers.get("content-length")
    log.debug("/download upstream HTTP %d content-length=%s headers=%s",
              upstream.status_code, content_length, dict(upstream.headers))

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=8192):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {}
    if content_length:
        headers["Content-Length"] = content_length
    # Suggest a meaningful filename for "Save to SD" so the device/user gets
    # firmware-<device>-<version>.bin instead of a generic name.
    if fid and "|" in fid:
        try:
            d, v, _ = parse_fid(fid)
            headers["Content-Disposition"] = f'attachment; filename="firmware-{d}-{v}.bin"'
        except ValueError:
            pass

    return StreamingResponse(
        stream_body(),
        media_type="application/octet-stream",
        headers=headers,
    )
