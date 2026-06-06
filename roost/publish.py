"""Static publish: turn a built directory into a real URL served from the
user's own control plane.

The publishing loop for someone who just *built* something (vibe coding) is the
bottleneck — they don't know how to put it online. Roost makes it one command:
``roost publish ./my-site`` tars the directory and POSTs it straight to
``/publish`` (one transactional call — nothing staged, nothing to dangle), and
the CP extracts it into ``<data_dir>/sites/<slug>/``, live immediately at
``GET /pub/<slug>/``. Publishing from an already-staged blob (two-step flow)
remains for worker-side jobs that uploaded via presign.

This module holds the pure helpers (slug normalization, safe extraction with
caps, the ``sites`` table rows, safe path resolution for serving). The
endpoints are wired in ``server.create_app`` next to the blob store.

Sites are on disk + in the ``sites`` table — no in-memory state — so they
survive control-plane restarts.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Any, Optional

# Caps: a publish target is a built site, not a filesystem. Reject bundles that
# blow past these before they can fill the disk.
SITE_MAX_BYTES = 256 * 1024 * 1024     # total uncompressed size
SITE_MAX_FILES = 5000                  # file count

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def sites_dir(db_path: Path) -> Path:
    d = db_path.parent / "sites"
    d.mkdir(parents=True, exist_ok=True)
    return d


def site_path(db_path: Path, slug: str) -> Path:
    return sites_dir(db_path) / slug


def normalize_slug(name: str) -> Optional[str]:
    """Lowercase, spaces→-, then validate against ^[a-z0-9][a-z0-9-]{0,39}$.

    Returns the slug, or None if it can't be made valid (caller → 400)."""
    if not name:
        return None
    slug = name.strip().lower().replace(" ", "-")
    if not _SLUG_RE.match(slug):
        return None
    return slug


def _is_within(child: Path, parent: Path) -> bool:
    """True iff resolved ``child`` is inside resolved ``parent`` (belt-and-braces
    on top of tarfile's data filter and the serving resolve check)."""
    try:
        parent_r = parent.resolve()
        child_r = child.resolve()
        return os.path.commonpath([str(parent_r), str(child_r)]) == str(parent_r)
    except (ValueError, OSError):
        return False


def _measure(tar: tarfile.TarFile) -> tuple[int, int]:
    """(total_bytes, file_count) of the regular files in a tar, for cap checks."""
    total = 0
    files = 0
    for m in tar.getmembers():
        if m.isreg():
            total += m.size
            files += 1
    return total, files


class PublishError(Exception):
    """Raised on cap/format violations; carries an HTTP status for the caller."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def extract_bundle(db_path: Path, slug: str, tar_gz_path: Path) -> tuple[int, int]:
    """SAFELY extract a tar.gz bundle into ``sites/<slug>/``, atomically.

    Uses Python 3.12 ``tarfile`` with ``filter="data"`` (rejects absolute paths,
    ``..``, devices; strips dangerous bits). Enforces caps, extracts into a temp
    dir, then ``os.replace`` swaps it over any existing site (the rebuild →
    republish loop). Returns (size_bytes, file_count).
    """
    try:
        tar = tarfile.open(tar_gz_path, mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise PublishError(400, f"not a valid tar.gz bundle: {e}")

    with tar:
        size, files = _measure(tar)
        if size > SITE_MAX_BYTES:
            raise PublishError(413, f"bundle exceeds {SITE_MAX_BYTES} bytes uncompressed")
        if files > SITE_MAX_FILES:
            raise PublishError(413, f"bundle exceeds {SITE_MAX_FILES} files")

        base = sites_dir(db_path)
        tmp = base / f".tmp-{slug}-{secrets.token_hex(6)}"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            # filter="data" is the security boundary: it drops members whose
            # path escapes the destination (absolute / ..) and strips devices,
            # setuid bits, links-out, etc. Belt-and-braces _is_within below.
            try:
                tar.extractall(tmp, filter="data")
            except (tarfile.TarError, OSError) as e:
                raise PublishError(400, f"unsafe or corrupt bundle: {e}")
            for p in tmp.rglob("*"):
                if p.is_file() and not _is_within(p, tmp):
                    raise PublishError(400, "bundle member escapes the site directory")

            dest = site_path(db_path, slug)
            old_swap = base / f".old-{slug}-{secrets.token_hex(6)}"
            if dest.exists():
                os.replace(dest, old_swap)
            os.replace(tmp, dest)
            if old_swap.exists():
                shutil.rmtree(old_swap, ignore_errors=True)
        except PublishError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise PublishError(500, f"could not install site: {e}")

    return size, files


def resolve_served_path(db_path: Path, slug: str, path: str) -> Optional[Path]:
    """Map a public request to a real file inside ``sites/<slug>/``, or None.

    Empty path / trailing slash → index.html. The final real path must be inside
    the site dir (resolve + commonpath). For SPA-ish sites: if the exact file is
    missing and the request has no file extension, fall back to index.html.
    Returns an existing file Path, or None for 404.
    """
    root = site_path(db_path, slug)
    if not root.is_dir():
        return None
    rel = (path or "").strip("/")
    if not rel:
        rel = "index.html"
    candidate = root / rel
    if not _is_within(candidate, root):
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
        if not _is_within(candidate, root):
            return None
    if candidate.is_file():
        return candidate
    # SPA fallback: extension-less route that doesn't map to a file → index.html.
    if "." not in Path(rel).name:
        index = root / "index.html"
        if index.is_file():
            return index
    return None


# ---------- rows ----------


def upsert_site(
    conn: sqlite3.Connection, slug: str, size: int, file_count: int, created_by: str
) -> dict[str, Any]:
    """Insert or replace a site row. Re-publishing updates updated_at but keeps
    the original created_at (it's the same site, rebuilt)."""
    now = time.time()
    existing = conn.execute(
        "SELECT created_at FROM sites WHERE slug=?", (slug,)
    ).fetchone()
    created_at = existing["created_at"] if existing is not None else now
    conn.execute(
        "INSERT INTO sites(slug, size, file_count, created_at, updated_at, created_by) "
        "VALUES (:slug, :size, :file_count, :created_at, :updated_at, :created_by) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "size=:size, file_count=:file_count, updated_at=:updated_at, created_by=:created_by",
        {
            "slug": slug,
            "size": size,
            "file_count": file_count,
            "created_at": created_at,
            "updated_at": now,
            "created_by": created_by,
        },
    )
    return {
        "slug": slug,
        "size": size,
        "file_count": file_count,
        "created_at": created_at,
        "updated_at": now,
        "created_by": created_by,
    }


def get_site(conn: sqlite3.Connection, slug: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row is not None else None


def list_sites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM sites ORDER BY updated_at DESC")
    return [dict(r) for r in cur.fetchall()]


def delete_site(db_path: Path, conn: sqlite3.Connection, slug: str) -> bool:
    cur = conn.execute("DELETE FROM sites WHERE slug=?", (slug,))
    shutil.rmtree(site_path(db_path, slug), ignore_errors=True)
    return cur.rowcount > 0


# ---------- public shapes ----------


def public_dict(
    row: dict[str, Any], base_url: str, publish_domain: Optional[str] = None
) -> dict[str, Any]:
    base = base_url.rstrip("/")
    out = {
        "slug": row["slug"],
        "url": f"{base}/pub/{row['slug']}/",
        "files": row["file_count"],
        "size": row["size"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if publish_domain:
        # The internet-facing address (slug-as-subdomain through the tunnel).
        out["public_url"] = f"https://{row['slug']}.{publish_domain}/"
    return out


def slug_for_host(host: str, publish_domain: str) -> Optional[str]:
    """Map a public Host header to a site slug, or None.

    ``demo.roost.pub`` → ``demo``; the apex and anything that isn't exactly
    ``<valid-slug>.<publish_domain>`` returns None. Slugs are already valid DNS
    labels by construction (^[a-z0-9][a-z0-9-]{0,39}$), so this is a pure
    suffix-strip + re-validation — no registry lookup needed to *route*.
    """
    hostname = host.split(":", 1)[0].strip().lower().rstrip(".")
    suffix = "." + publish_domain.lower()
    if not hostname.endswith(suffix):
        return None
    label = hostname[: -len(suffix)]
    if "." in label:  # only one level deep: a.b.roost.pub is not a site
        return None
    return label if _SLUG_RE.match(label) else None
