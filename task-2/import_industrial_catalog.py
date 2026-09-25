#!/usr/bin/env python3
"""
Industrial catalog setup for Adobe Commerce as a Cloud Service (ACCS).

Creates product attributes from industrial-products.csv, assigns them to the
Default attribute set, and optionally patches product attribute values.

See README.md for credentials, full workflow, and troubleshooting.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("Install dependencies: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = SCRIPT_DIR / "data" / "industrial-products.csv"

DEMO_SKUS = {"HEC-2002", "ISS-4004", "PSV-3003", "HDP-1001"}

DEMO_ATTR_KEYS = {
    "horsepower",
    "voltage",
    "phase",
    "rpm",
    "efficiency_rating",
    "frame_size",
    "safety_rating",
    "switch_type",
    "contacts",
    "housing_material",
    "connection_type",
    "port_size",
    "operating_pressure",
    "media",
    "response_time",
    "max_pressure",
    "bore_diameter",
    "stroke_length",
    "mounting_type",
}

DEFAULT_SCOPES = (
    "openid,AdobeID,email,profile,additional_info.roles,"
    "additional_info.projectedProductContext,commerce.accs"
)

SKIP_ATTRIBUTE_CODES = frozenset(
    {
        "media",
        "links",
        "category",
        "category_ids",
        "price",
        "status",
        "visibility",
        "tax_class_id",
        "quantity_and_stock_status",
    }
)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        print(f"Missing env var: {name} (set in .env — see README.md)", file=sys.stderr)
        sys.exit(1)
    return value


def label_from_code(code: str) -> str:
    return code.replace("_", " ").strip().title()


def search_criteria_url(path: str, filters: List[Tuple[str, str, str]]) -> str:
    params: List[Tuple[str, str]] = []
    for idx, (field, value, condition) in enumerate(filters):
        prefix = f"searchCriteria[filterGroups][0][filters][{idx}]"
        params.extend(
            [
                (f"{prefix}[field]", field),
                (f"{prefix}[value]", value),
                (f"{prefix}[conditionType]", condition),
            ]
        )
    return f"{path}?{urlencode(params)}"


def parse_additional_attributes(raw: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not raw:
        return result
    for part in raw.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            result[key] = value
    return result


def collect_attribute_codes(csv_path: Path, demo_only: bool) -> Set[str]:
    codes: Set[str] = set()
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            attrs = parse_additional_attributes(row.get("additional_attributes", ""))
            if demo_only and row.get("sku") not in DEMO_SKUS:
                continue
            codes.update(attrs.keys())
    if demo_only:
        codes &= DEMO_ATTR_KEYS
    return codes


def collect_sku_attributes(csv_path: Path) -> Dict[str, Dict[str, str]]:
    by_sku: Dict[str, Dict[str, str]] = {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            sku = (row.get("sku") or "").strip()
            if not sku:
                continue
            by_sku[sku] = parse_additional_attributes(row.get("additional_attributes", ""))
    return by_sku


class AccsClient:
    def __init__(self, dry_run: bool = False, pause_ms: int = 150):
        self.dry_run = dry_run
        self.pause_sec = pause_ms / 1000.0
        self.base_url = env("COMMERCE_API_BASE_URL").rstrip("/")
        self.client_id = env("IMS_OAUTH_S2S_CLIENT_ID")
        self.client_secret = env("IMS_OAUTH_S2S_CLIENT_SECRET")
        self.org_id = env("IMS_OAUTH_S2S_ORG_ID")
        self.store_scope = os.environ.get("COMMERCE_STORE_SCOPE", "all")
        self.token_url = os.environ.get("IMS_TOKEN_URL", "https://ims-na1.adobelogin.com/ims/token/v3")
        self.scopes = os.environ.get("IMS_OAUTH_S2S_SCOPES", DEFAULT_SCOPES)
        self._token: Optional[str] = None
        self.session = requests.Session()

    def _sleep(self) -> None:
        if self.pause_sec > 0:
            time.sleep(self.pause_sec)

    def get_token(self) -> str:
        if self._token:
            return self._token
        if self.dry_run:
            self._token = "dry-run-token"
            return self._token
        resp = self.session.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scopes,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        return self._token

    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "x-api-key": self.client_id,
            "x-gw-ims-org-id": self.org_id,
            "Content-Type": "application/json",
            "Store": self.store_scope,
        }

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        if self.dry_run and method.upper() != "GET":
            print(f"  [dry-run] {method.upper()} {path}")

            class FakeResp:
                status_code = 200

                @staticmethod
                def json():
                    return {}

            self._sleep()
            return FakeResp()  # type: ignore[return-value]

        resp = self.session.request(method, url, headers=self.headers(), timeout=120, **kwargs)
        self._sleep()
        return resp

    def get_json(self, path: str) -> dict:
        resp = self.request("GET", path)
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {path} failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def attribute_exists(self, code: str) -> bool:
        resp = self.request("GET", f"/V1/products/attributes/{code}")
        return resp.status_code == 200

    def create_attribute(self, code: str) -> Tuple[str, bool]:
        if self.attribute_exists(code):
            return code, False
        payload = {
            "attribute": {
                "attribute_code": code,
                "frontend_input": "text",
                "entity_type_id": 4,
                "is_required": False,
                "is_user_defined": True,
                "is_visible": True,
                "is_visible_on_front": True,
                "is_searchable": False,
                "is_filterable": False,
                "is_comparable": False,
                "scope": "global",
                "default_frontend_label": label_from_code(code),
                "backend_type": "varchar",
                "used_in_product_listing": True,
            }
        }
        resp = self.request("POST", "/V1/products/attributes", json=payload)
        if resp.status_code in (200, 201):
            return code, True
        if resp.status_code == 400 and "already exists" in resp.text.lower():
            return code, False
        raise RuntimeError(f"Create attribute {code} failed ({resp.status_code}): {resp.text[:500]}")

    def resolve_default_attribute_set(self) -> Tuple[int, int]:
        attribute_set_id = self._resolve_default_attribute_set_id()
        group_items = self._list_attribute_groups(attribute_set_id)
        if not group_items:
            raise RuntimeError(f"No groups for attribute set id={attribute_set_id}")

        for preferred in ("General", "Product Details", "Attributes"):
            for group in group_items:
                if group.get("attribute_group_name") == preferred:
                    return attribute_set_id, int(group["attribute_group_id"])

        return attribute_set_id, int(group_items[0]["attribute_group_id"])

    def _resolve_default_attribute_set_id(self) -> int:
        endpoints = [
            search_criteria_url(
                "/V1/products/attribute-sets/sets/list",
                [("attribute_set_name", "Default", "eq")],
            ),
            search_criteria_url(
                "/V1/eav/attribute-sets/list",
                [("attribute_set_name", "Default", "eq")],
            ),
        ]
        last_error: Optional[Exception] = None
        for path in endpoints:
            try:
                data = self.get_json(path)
                items = data.get("items") or []
                if items:
                    return int(items[0]["attribute_set_id"])
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError("Could not find Default attribute set") from last_error

    def _list_attribute_groups(self, attribute_set_id: int) -> List[dict]:
        path = search_criteria_url(
            "/V1/products/attribute-sets/groups/list",
            [("attribute_set_id", str(attribute_set_id), "eq")],
        )
        data = self.get_json(path)
        return data.get("items") or []

    def assign_attribute_to_set(self, attribute_set_id: int, group_id: int, code: str, sort_order: int) -> bool:
        payload = {
            "attributeSetId": attribute_set_id,
            "attributeGroupId": group_id,
            "attributeCode": code,
            "sortOrder": sort_order,
        }
        paths = [
            f"/V1/products/attribute-sets/{attribute_set_id}/attributes",
            "/V1/products/attribute-sets/attributes",
        ]
        last_body = ""
        for path in paths:
            resp = self.request("POST", path, json=payload)
            last_body = resp.text[:500]
            if resp.status_code in (200, 201):
                return True
            if resp.status_code == 400 and "already" in resp.text.lower():
                return False
            if resp.status_code == 404:
                continue
        raise RuntimeError(
            f"Assign {code} to set {attribute_set_id} failed: {last_body}"
        )

    def get_product(self, sku: str) -> Optional[dict]:
        encoded = requests.utils.quote(sku, safe="")
        resp = self.request("GET", f"/V1/products/{encoded}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"GET product {sku} failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def patch_product_attributes(self, sku: str, attrs: Dict[str, str]) -> bool:
        existing = self.get_product(sku)
        if not existing:
            print(f"  skip {sku}: product not found (import CSV in Admin first?)")
            return False

        product = existing if "sku" in existing else existing.get("product", existing)
        current = {
            item["attribute_code"]: item.get("value")
            for item in (product.get("custom_attributes") or [])
            if item.get("attribute_code")
        }
        for key, value in attrs.items():
            current[key] = value

        payload = {
            "product": {
                "sku": sku,
                "custom_attributes": [
                    {"attribute_code": k, "value": v} for k, v in sorted(current.items()) if v is not None
                ],
            }
        }
        encoded = requests.utils.quote(sku, safe="")
        resp = self.request("PUT", f"/V1/products/{encoded}", json=payload)
        if resp.status_code in (200, 201):
            return True
        raise RuntimeError(f"PUT product {sku} failed ({resp.status_code}): {resp.text[:500]}")


def cmd_verify(client: AccsClient) -> None:
    print("Verifying IMS token and ACCS REST access...")
    client.get_token()
    print("  IMS token: OK")
    set_id, group_id = client.resolve_default_attribute_set()
    print(f"  Default attribute set: id={set_id}, General group id={group_id}")
    resp = client.request("GET", "/V1/products/attributes/manufacturer")
    if resp.status_code == 200:
        print("  Sample attribute lookup (manufacturer): OK")
    else:
        print(f"  Sample attribute lookup returned {resp.status_code} (may still be fine)")
    print("\nCredentials look good. Run: python import_industrial_catalog.py --all")


def cmd_create_attributes(client: AccsClient, codes: Iterable[str]) -> None:
    created = skipped = failed = 0
    failed_codes: List[str] = []
    for code in sorted(codes):
        if code in SKIP_ATTRIBUTE_CODES:
            print(f"  skip reserved/blocked code: {code}")
            skipped += 1
            continue
        if not re.match(r"^[a-z][a-z0-9_]*$", code):
            print(f"  skip invalid attribute code: {code}")
            failed += 1
            failed_codes.append(code)
            continue
        try:
            _, was_created = client.create_attribute(code)
            if was_created:
                print(f"  created: {code}")
                created += 1
            else:
                print(f"  exists:  {code}")
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED:  {code} — {exc}")
            failed += 1
            failed_codes.append(code)
    print(f"\nAttributes: created={created}, exists/skipped={skipped}, failed={failed}")
    if failed_codes:
        print(f"Failed codes: {', '.join(failed_codes)}")


def cmd_assign_to_set(client: AccsClient, codes: Iterable[str]) -> None:
    attribute_set_id, group_id = client.resolve_default_attribute_set()
    print(f"Default attribute set id={attribute_set_id}, group id={group_id}")
    assigned = skipped = failed = 0
    for idx, code in enumerate(sorted(codes)):
        if code in SKIP_ATTRIBUTE_CODES:
            skipped += 1
            continue
        if not client.attribute_exists(code):
            print(f"  skip (no attribute): {code}")
            skipped += 1
            continue
        try:
            ok = client.assign_attribute_to_set(attribute_set_id, group_id, code, 100 + idx)
            if ok:
                print(f"  assigned: {code}")
                assigned += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED:  {code} — {exc}")
            failed += 1
    print(f"\nAssign: assigned={assigned}, skipped={skipped}, failed={failed}")


def cmd_patch_products(client: AccsClient, by_sku: Dict[str, Dict[str, str]], skus: Optional[Set[str]]) -> None:
    updated = missing = failed = 0
    for sku, attrs in sorted(by_sku.items()):
        if skus and sku not in skus:
            continue
        if not attrs:
            continue
        try:
            result = client.patch_product_attributes(sku, attrs)
            if result:
                print(f"  patched: {sku} ({len(attrs)} attributes)")
                updated += 1
            else:
                missing += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED:  {sku} — {exc}")
            failed += 1
    print(f"\nPatch: updated={updated}, missing={missing}, failed={failed}")


def print_admin_import_reminder(csv_path: Path) -> None:
    print(
        f"""
----------------------------------------------------------------------
Next: import products in Commerce Admin
----------------------------------------------------------------------
1. Admin → System → Data Transfer → Import
2. Entity Type: Products | Behavior: Add/Update | Validation: Stop on Error
3. File: {csv_path}
4. Check Data → Import
5. Wait 5–10 min for Catalog Service sync
6. PDP console: events.lastPayload('pdp/data')?.attributes

If products already exist but lack spec values, run:
  python import_industrial_catalog.py --patch-product-attributes
----------------------------------------------------------------------
"""
    )


def main() -> None:
    load_dotenv(SCRIPT_DIR / ".env")

    parser = argparse.ArgumentParser(
        description="Create industrial catalog attributes on ACCS and prepare CSV import",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python import_industrial_catalog.py --verify
  python import_industrial_catalog.py --all
  python import_industrial_catalog.py --assign-to-default-set
  python import_industrial_catalog.py --all --demo-keys-only
  python import_industrial_catalog.py --patch-product-attributes --skus HEC-2002
""",
    )
    parser.add_argument("--csv", type=Path, default=Path(os.environ.get("INDUSTRIAL_CSV", DEFAULT_CSV)))
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    parser.add_argument("--verify", action="store_true", help="Test IMS + REST credentials only")
    parser.add_argument("--demo-keys-only", action="store_true", help="Only demo SKU attribute keys (~20)")
    parser.add_argument("--create-attributes", action="store_true")
    parser.add_argument("--assign-to-default-set", action="store_true")
    parser.add_argument("--patch-product-attributes", action="store_true")
    parser.add_argument("--skus", nargs="*", help="Limit patch to these SKUs")
    parser.add_argument("--all", action="store_true", help="create-attributes + assign + import reminder")
    parser.add_argument("--pause-ms", type=int, default=150, help="Delay between API calls")
    args = parser.parse_args()

    if not args.csv.is_file():
        print(f"CSV not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    if args.all:
        args.create_attributes = True
        args.assign_to_default_set = True

    if not any(
        [
            args.verify,
            args.create_attributes,
            args.assign_to_default_set,
            args.patch_product_attributes,
            args.all,
        ]
    ):
        parser.print_help()
        sys.exit(1)

    client = AccsClient(dry_run=args.dry_run)

    try:
        if not args.dry_run:
            client.get_token()
            if not args.verify:
                print("IMS token acquired.\n")
    except Exception as exc:  # noqa: BLE001
        print(f"Authentication failed: {exc}", file=sys.stderr)
        print("Check .env values — see README.md § Credentials", file=sys.stderr)
        sys.exit(1)

    if args.verify:
        cmd_verify(client)
        return

    codes = collect_attribute_codes(args.csv, demo_only=args.demo_keys_only)
    print(f"CSV: {args.csv}")
    print(f"Attribute codes to process: {len(codes)}" + (" (demo subset)" if args.demo_keys_only else ""))

    if args.create_attributes:
        print("=== Create product attributes ===")
        cmd_create_attributes(client, codes)

    if args.assign_to_default_set:
        print("\n=== Assign attributes to Default attribute set ===")
        cmd_assign_to_set(client, codes)

    if args.patch_product_attributes:
        print("\n=== Patch product custom attributes from CSV ===")
        by_sku = collect_sku_attributes(args.csv)
        sku_filter = set(args.skus) if args.skus else None
        cmd_patch_products(client, by_sku, sku_filter)

    if args.all:
        print_admin_import_reminder(args.csv.resolve())


if __name__ == "__main__":
    main()
