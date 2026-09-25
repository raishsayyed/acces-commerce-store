#!/usr/bin/env python3
"""
Upload local product images to Adobe Commerce (ACCS) via REST.

Place one image per SKU in data/images/, named after the SKU, for example:
  data/images/HDP-1001.jpg
  data/images/HEC-2002.png

Each upload is assigned to base, small, and thumbnail roles (Commerce types:
image, small_image, thumbnail).

Requires the same .env credentials as import_industrial_catalog.py.
Products must already exist in Commerce (import CSV in Admin first).
"""

from __future__ import annotations

import argparse
import base64
import csv
import mimetypes
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, Union

import requests

from import_industrial_catalog import (
    DEFAULT_CSV,
    SCRIPT_DIR,
    AccsClient,
    load_dotenv,
)

DEFAULT_IMAGES_DIR = SCRIPT_DIR / "data" / "images"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Commerce media "types" → Admin roles base_image, small_image, thumbnail_image
IMAGE_ROLES = ("image", "small_image", "thumbnail")


def parse_media_upload_response(body: Any) -> Tuple[Optional[int], Optional[str]]:
    """
    Commerce may return a media entry object, a numeric id, or a file path string.
    ACCS often returns only the relative path (e.g. "/a/c/acm-3030.jpg") on success.
    """
    if isinstance(body, dict):
        media_id = body.get("id")
        if media_id is None and isinstance(body.get("entry"), dict):
            media_id = body["entry"].get("id")
        file_path = body.get("file")
        if file_path is None and isinstance(body.get("entry"), dict):
            file_path = body["entry"].get("file")
        return (int(media_id) if media_id is not None else None, file_path)
    if isinstance(body, int):
        return body, None
    if isinstance(body, str) and body.strip():
        return None, body.strip()
    return None, None


def mime_for_path(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")


def collect_skus_from_csv(csv_path: Path) -> Set[str]:
    skus: Set[str] = set()
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            sku = (row.get("sku") or "").strip()
            if sku:
                skus.add(sku)
    return skus


def discover_images(images_dir: Path) -> Dict[str, Path]:
    """Map SKU → image path (first match per stem if multiple extensions exist)."""
    found: Dict[str, Path] = {}
    if not images_dir.is_dir():
        return found
    for path in sorted(images_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        sku = path.stem
        if sku not in found:
            found[sku] = path
    return found


def build_media_payload(path: Path, label: Optional[str] = None) -> dict:
    raw = path.read_bytes()
    return {
        "entry": {
            "media_type": "image",
            "label": label or path.stem.replace("-", " "),
            "position": 1,
            "disabled": False,
            "types": list(IMAGE_ROLES),
            "content": {
                "base64_encoded_data": base64.b64encode(raw).decode("ascii"),
                "type": mime_for_path(path),
                "name": path.name,
            },
        }
    }


class ImageUploadClient(AccsClient):
    def product_exists(self, sku: str) -> bool:
        return self.get_product(sku) is not None

    def upload_product_image(
        self, sku: str, image_path: Path, label: Optional[str] = None
    ) -> Union[dict, str, int]:
        encoded = requests.utils.quote(sku, safe="")
        path = f"/V1/products/{encoded}/media"
        payload = build_media_payload(image_path, label=label)

        if self.dry_run:
            print(f"  [dry-run] POST {path} ({image_path.name}, roles={IMAGE_ROLES})")
            return {"id": 0}

        resp = self.request("POST", path, json=payload)
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(
            f"POST media for {sku} failed ({resp.status_code}): {resp.text[:500]}"
        )


def cmd_list(images_dir: Path, csv_path: Path, use_csv: bool) -> int:
    images = discover_images(images_dir)
    expected = collect_skus_from_csv(csv_path) if use_csv else set(images.keys())
    if not use_csv:
        expected = set(images.keys())

    missing_files = sorted(expected - images.keys())
    extra_files = sorted(images.keys() - expected) if use_csv else []

    print(f"Images directory: {images_dir}")
    print(f"Image files found: {len(images)}")
    if use_csv:
        print(f"SKUs in CSV: {len(expected)}")
        print(f"Ready to upload (CSV SKU + local file): {len(expected & images.keys())}")

    if images:
        print("\nLocal files:")
        for sku in sorted(images):
            print(f"  {sku} → {images[sku].name}")

    if missing_files:
        print(f"\nMissing image file ({len(missing_files)} SKUs):")
        for sku in missing_files[:20]:
            print(f"  {sku} (expected e.g. {sku}.jpg)")
        if len(missing_files) > 20:
            print(f"  ... and {len(missing_files) - 20} more")

    if extra_files:
        print(f"\nFiles with no matching CSV SKU ({len(extra_files)}):")
        for sku in extra_files[:10]:
            print(f"  {sku}")
        if len(extra_files) > 10:
            print(f"  ... and {len(extra_files) - 10} more")

    return 0 if not missing_files or not use_csv else 1


def cmd_upload(
    client: ImageUploadClient,
    images_dir: Path,
    csv_path: Path,
    use_csv: bool,
    skus: Optional[Set[str]],
    skip_missing_product: bool,
) -> int:
    images = discover_images(images_dir)
    if not images:
        print(f"No image files in {images_dir}", file=sys.stderr)
        print(f"Supported extensions: {', '.join(sorted(IMAGE_EXTENSIONS))}", file=sys.stderr)
        return 1

    if use_csv:
        csv_skus = collect_skus_from_csv(csv_path)
        work = {sku: path for sku, path in images.items() if sku in csv_skus}
    else:
        work = dict(images)

    if skus:
        work = {sku: path for sku, path in work.items() if sku in skus}

    if not work:
        print("Nothing to upload (check --skus, --from-csv, or image filenames).", file=sys.stderr)
        return 1

    uploaded = skipped = missing_product = failed = 0

    print(f"Uploading {len(work)} image(s) to Commerce (roles: {', '.join(IMAGE_ROLES)})...\n")

    for sku in sorted(work):
        path = work[sku]
        try:
            if not client.product_exists(sku):
                print(f"  skip {sku}: product not found in Commerce")
                missing_product += 1
                if skip_missing_product:
                    skipped += 1
                    continue
                return 1
            body = client.upload_product_image(sku, path)
            media_id, file_path = parse_media_upload_response(body)
            detail = ""
            if media_id is not None:
                detail = f" (media id {media_id})"
            elif file_path:
                detail = f" ({file_path})"
            print(f"  uploaded: {sku} ← {path.name}{detail}")
            uploaded += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED:  {sku} — {exc}")
            failed += 1

    print(f"\nDone: uploaded={uploaded}, skipped={skipped}, missing_product={missing_product}, failed={failed}")
    return 1 if failed else 0


def cmd_verify(client: ImageUploadClient) -> None:
    print("Verifying IMS token and product API access...")
    client.get_token()
    print("  IMS token: OK")
    sample = client.request("GET", "/V1/products/attributes/manufacturer")
    if sample.status_code == 200:
        print("  REST catalog access: OK")
    else:
        print(f"  REST sample returned {sample.status_code}")
    print("\nReady. Example:")
    print("  python upload_product_images.py --list --from-csv")
    print("  python upload_product_images.py --upload --from-csv")


def main() -> None:
    load_dotenv(SCRIPT_DIR / ".env")

    parser = argparse.ArgumentParser(
        description="Upload data/images files to Commerce product media (base/small/thumbnail)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Image naming:
  data/images/<SKU>.jpg   (or .jpeg, .png, .webp, .gif)

Examples:
  python upload_product_images.py --verify
  python upload_product_images.py --list --from-csv
  python upload_product_images.py --upload --from-csv
  python upload_product_images.py --upload --skus HDP-1001 HEC-2002
  python upload_product_images.py --upload --dry-run
""",
    )
    parser.add_argument("--images-dir", type=Path, default=DEFAULT_IMAGES_DIR)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument(
        "--from-csv",
        action="store_true",
        help="Only upload SKUs present in industrial-products.csv",
    )
    parser.add_argument("--verify", action="store_true", help="Test credentials only")
    parser.add_argument("--list", action="store_true", help="Show files vs CSV SKUs")
    parser.add_argument("--upload", action="store_true", help="Upload images to Commerce")
    parser.add_argument("--skus", nargs="*", help="Limit upload to these SKUs")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without POST")
    parser.add_argument(
        "--skip-missing-product",
        action="store_true",
        help="Skip SKUs not in Commerce instead of exiting on first missing",
    )
    parser.add_argument("--pause-ms", type=int, default=150, help="Delay between API calls")
    args = parser.parse_args()

    if not any([args.verify, args.list, args.upload]):
        parser.print_help()
        sys.exit(1)

    if args.upload or args.list:
        args.images_dir.mkdir(parents=True, exist_ok=True)

    client = ImageUploadClient(dry_run=args.dry_run, pause_ms=args.pause_ms)

    if args.verify:
        try:
            cmd_verify(client)
        except Exception as exc:  # noqa: BLE001
            print(f"Verification failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.list:
        sys.exit(cmd_list(args.images_dir, args.csv, args.from_csv))

    if args.upload:
        if not args.csv.is_file() and args.from_csv:
            print(f"CSV not found: {args.csv}", file=sys.stderr)
            sys.exit(1)
        try:
            if not args.dry_run:
                client.get_token()
                print("IMS token acquired.\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Authentication failed: {exc}", file=sys.stderr)
            sys.exit(1)

        sku_filter = set(args.skus) if args.skus else None
        sys.exit(
            cmd_upload(
                client,
                args.images_dir,
                args.csv,
                args.from_csv,
                sku_filter,
                args.skip_missing_product,
            )
        )


if __name__ == "__main__":
    main()
