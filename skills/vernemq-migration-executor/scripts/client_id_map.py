#!/usr/bin/env python3
"""Maps source-broker client IDs to ConnectMQ client IDs.

ConnectMQ accepts a connection when the client ID is the credential's issued client ID or "<ISSUED_ID>_<suffix>".
This script builds the suffix from the old client ID and flags IDs that will cause trouble.

Input: a CSV with a client ID column (default "client_id"), or a plain text file with one ID per line.
Optional column "credential" assigns a device to a specific issued client ID (one credential per group/site).

Examples:
  python client_id_map.py clients.csv --issued-id c7f3k2 -o client-map.csv
  python client_id_map.py clients.txt --issued-id c7f3k2 --sanitize
  python client_id_map.py clients.csv --credential-column site    # issued ID per row from the "site" column

Output columns: old_client_id,new_client_id,issued_id,status,notes   (status: ok | changed | error)
Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter

SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def read_ids(path: str, column: str, cred_column: str | None) -> list[tuple[str, str | None]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    first = text.splitlines()[0] if text else ""
    if column in [c.strip() for c in first.split(",")]:
        rows = csv.DictReader(text.splitlines())
        return [(r[column].strip(), (r.get(cred_column) or "").strip() or None if cred_column else None)
                for r in rows if r.get(column, "").strip()]
    return [(line.strip(), None) for line in text.splitlines() if line.strip() and not line.startswith("#")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="CSV or text file with the old client IDs")
    ap.add_argument("--issued-id", help="Issued client ID of the ConnectMQ credential the devices will share")
    ap.add_argument("--column", default="client_id", help="CSV column with the old client ID (default client_id)")
    ap.add_argument("--credential-column", help="CSV column holding the issued client ID per row")
    ap.add_argument("--sanitize", action="store_true",
                    help="Replace characters outside [A-Za-z0-9._-] with '-' instead of reporting an error")
    ap.add_argument("--max-length", type=int, default=128,
                    help="Warn when the new client ID is longer than this (default 128; many device SDKs cap it)")
    ap.add_argument("-o", "--output", help="Output CSV (default stdout)")
    args = ap.parse_args()

    if not args.issued_id and not args.credential_column:
        ap.error("give --issued-id or --credential-column")

    rows = read_ids(args.input, args.column, args.credential_column)
    results = []
    for old, cred in rows:
        issued = cred or args.issued_id
        notes, status = [], "ok"
        suffix = old
        if not SAFE.match(old):
            if args.sanitize:
                suffix = re.sub(r"[^A-Za-z0-9._-]", "-", old)
                status = "changed"
                notes.append("unsafe characters replaced")
            else:
                status = "error"
                notes.append("contains characters outside [A-Za-z0-9._-] (spaces, '/', '+', '#' or non-ASCII); use --sanitize")
        if old.startswith(issued + "_"):
            suffix = old[len(issued) + 1:]
            notes.append("already prefixed")
        new = issued if suffix == "" else f"{issued}_{suffix}"
        if len(new) > args.max_length:
            notes.append(f"new ID is {len(new)} chars (> {args.max_length})")
            if status == "ok":
                status = "changed"
        results.append({"old_client_id": old, "new_client_id": new, "issued_id": issued,
                        "status": status, "notes": "; ".join(notes)})

    # Collisions: two old IDs mapping to one new ID (sanitizing) or IDs that differ only by case
    counts = Counter(r["new_client_id"] for r in results)
    lower = Counter(r["new_client_id"].lower() for r in results)
    for r in results:
        if counts[r["new_client_id"]] > 1:
            r["status"] = "error"
            r["notes"] = (r["notes"] + "; " if r["notes"] else "") + "duplicate new client ID (session takeover risk)"
        elif lower[r["new_client_id"].lower()] > 1:
            r["notes"] = (r["notes"] + "; " if r["notes"] else "") + "differs from another ID only by case"

    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    w = csv.DictWriter(out, fieldnames=["old_client_id", "new_client_id", "issued_id", "status", "notes"])
    w.writeheader()
    w.writerows(results)
    if args.output:
        out.close()

    errors = sum(r["status"] == "error" for r in results)
    print(f"{len(results)} client IDs, {errors} errors, "
          f"{sum(r['status'] == 'changed' for r in results)} changed", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
