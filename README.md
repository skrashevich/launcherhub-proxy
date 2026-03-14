# LauncherHub Proxy

A LauncherHub-compatible API proxy that aggregates Meshtastic firmware from all available repositories and automatically discovers related device variants using a family-based name-matching algorithm.

It acts as a drop-in replacement for `api.launcherhub.net`, allowing the [Meshtastic Launcher](https://github.com/meshtastic/launcher) to query a self-hosted server that proxies and enriches data from [mrekin.duckdns.org/flasher](https://mrekin.duckdns.org/flasher).

---

## Features

- **Multi-source aggregation** — queries every firmware source available on the upstream and merges the results into a single list.
- **Family-based device matching** — automatically discovers firmware variants that share the same device family (e.g. querying `t-deck-plus` also returns `t-deck`, `t-deck-tft`, `t-deck-ru`, …).
- **Partition-table parsing** — reads the ESP32 partition table from the firmware binary and returns the partition metadata expected by the Launcher (`nb`, `ao`, `as`, `s`, `so`, `ss`, `f`, `fo`, `fs`, `f2`, `fo2`, `fs2`).
- **Transparent download proxy** — streams firmware binaries from upstream to the Launcher with proper `Content-Length` and `Content-Disposition` headers.
- **In-memory caching** — sources and device lists are cached for the lifetime of the process to reduce upstream round-trips.
- **HTTPS-ready** — can be run behind a TLS terminator or directly via uvicorn's `--ssl-*` flags.

---

## Requirements

- Python 3.10+
- [FastAPI](https://fastapi.tiangolo.com/) ≥ 0.110.0
- [Uvicorn](https://www.uvicorn.org/) ≥ 0.29.0 (with standard extras)
- [httpx](https://www.python-httpx.org/) ≥ 0.27.0

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Quick Start

### Run locally

```bash
pip install fastapi "uvicorn[standard]" httpx
uvicorn main:app --host 0.0.0.0 --port 9945
```

The API will be available at `http://localhost:9945`.

### Run with HTTPS (required by the Launcher firmware's `WiFiClientSecure`)

```bash
uvicorn main:app --host 0.0.0.0 --port 443 \
    --ssl-keyfile key.pem --ssl-certfile cert.pem
```

### Run with Docker

Build and run the included `Dockerfile`:

```bash
docker build -t launcherhub-proxy .
docker run -d -p 9945:9945 launcherhub-proxy
```

Override the log level at runtime:

```bash
docker run -d -p 9945:9945 -e LOG_LEVEL=DEBUG launcherhub-proxy
```

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

---

## API Reference

### `GET /firmwares`

Returns a paginated list of available firmware for a device family **or** the full detail object for a single firmware entry.

#### List mode — query by device family

| Parameter | Type | Required | Description |
|---|---|---|---|
| `category` | string | ✓ | Device name or OTA tag (e.g. `t-deck`, `t-deck-plus`). Family matching is applied automatically. |
| `order_by` | string | | `downloads` (default, sorts newest-first by date) or `name`. |
| `page` | integer | | Page number (1-based, default `1`). |
| `q` | string | | Full-text search filter applied to `name` and `author` fields. |
| `star` | integer | | Set to `1` to return only starred items (currently none are starred). |

**Response**

```json
{
  "total": 42,
  "page_size": 10,
  "page": 1,
  "items": [
    {
      "fid": "t-deck|v2.7.18.fb3bf78|Official%20repo",
      "name": "t-deck v2.7.18.fb3bf78 [Official repo]",
      "author": "Official repo",
      "star": false
    }
  ]
}
```

#### Detail mode — query by `fid`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `fid` | string | ✓ | Firmware identifier in the format `<device>\|<version>[|<src>]`. |

**Response**

```json
{
  "fid": "t-deck|v2.7.18.fb3bf78|Official%20repo",
  "name": "t-deck v2.7.18.fb3bf78 [Official repo]",
  "author": "Official repo",
  "star": false,
  "versions": [
    {
      "version": "v2.7.18.fb3bf78",
      "published_at": "2024-05-01T12:00:00Z",
      "file": "https://mrekin.duckdns.org/flasher/api/firmware?t=t-deck&v=v2.7.18.fb3bf78&u=1&p=fw&e=true&src=Official%20repo",
      "zip_url": "https://mrekin.duckdns.org/flasher/api/firmware?t=t-deck&v=v2.7.18.fb3bf78&u=5&p=fw&e=true&src=Official%20repo",
      "Fs": 3582768,
      "nb": false,
      "ao": 65536,
      "as": 3407872,
      "s": true,
      "so": 3473408,
      "ss": 131072,
      "f": false,
      "fo": 0,
      "fs": 0,
      "f2": false,
      "fo2": 0,
      "fs2": 0
    }
  ]
}
```

> **Note:** `Fs` (capital F) is the total firmware file size in bytes, matching the field name used by the official LauncherHub API. The lowercase partition fields (`nb`, `ao`, `as`, `s`, `so`, `ss`, `f`, `fo`, `fs`, `f2`, `fo2`, `fs2`) are parsed from the ESP32 partition table embedded in the binary.

---

### `GET /download`

Streams a firmware binary from upstream to the client. Used by the Launcher to flash or save the file.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `fid` | string | recommended | Firmware identifier (`<device>\|<version>[|<src>]`). Used to reconstruct the correct upstream URL. |
| `file` | string | fallback | Raw upstream URL. Used only when `fid` is absent or cannot be parsed. |

The response is an `application/octet-stream` binary stream with `Content-Length` and a `Content-Disposition: attachment; filename="firmware-<device>-<version>.bin"` header.

---

## `fid` Format

The firmware identifier (`fid`) encodes the three coordinates needed to uniquely address a firmware entry:

```
<device>|<version>|<src>
```

| Field | Example | Notes |
|---|---|---|
| `device` | `t-deck` | Device name as known to the upstream API. |
| `version` | `v2.7.18.fb3bf78` | Version string. |
| `src` | `Official%20repo` | Percent-encoded source name. Defaults to `Official repo` when omitted. |

Examples:

```
t-deck|v2.7.18.fb3bf78|Official%20repo
t-deck-tft|v2.7.19.bb3d6d5|svk
```

---

## Family Matching Algorithm

1. **Find the family root** — the longest dash-separated prefix of the requested device name that is a known firmware device.  
   e.g. `t-deck-plus` → root `t-deck` (when `t-deck-plus` is unknown but `t-deck` is).

2. **Collect all devices sharing that root** — every known device whose name equals `<root>` or starts with `<root>-` / `<root>_`.  
   e.g. `t-deck` → `{t-deck, t-deck-tft, t-deck-tft-ru, t-deck-plus, t-deck-ru, …}`.

3. The requested device is always listed first in the result, followed by the other family members in alphabetical order.

---

## License

This project is licensed under the [Apache License 2.0](LICENSE).
