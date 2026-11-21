#!/usr/bin/env python3
"""Exports retained messages from a source broker and (optionally) re-publishes them as retained on ConnectMQ.

A bridge only carries messages published while it runs; retained state published before that (device status,
last configuration, shadow/twin state) must be copied once. Run it right before the first client wave and again just
before cutover.

  python copy_retained.py --source mqtt://old-broker:1883 --filter '#' --export retained.json
  python copy_retained.py --import retained.json --target mqtts://YOUR_BROKER_HOST:8883 --dry-run
  python copy_retained.py --source mqtt://old-broker:1883 --target mqtts://YOUR_BROKER_HOST:8883 --filter 'devices/#'

The export file is JSON: [{"topic": ..., "payload_b64": ..., "qos": ...}]. Other tools can produce it too
(for example a converted AWS shadow or Azure twin export).

Credentials: SOURCE_USERNAME / SOURCE_PASSWORD, CONNECTMQ_USERNAME / CONNECTMQ_PASSWORD / CONNECTMQ_CLIENT_ID.
Requires paho-mqtt 2.x.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import uuid

from mqtt_common import connect, make_client, parse_url, reason_text


def collect(args) -> list[dict]:
    ep = parse_url(args.source, args.source_username, args.source_password)
    client = make_client(ep, args.source_client_id, args.source_protocol, cafile=args.source_cafile,
                         certfile=args.source_cert, keyfile=args.source_key)
    found: dict[str, dict] = {}
    last_seen = [time.time()]
    ready = threading.Event()

    def on_connect(c, u, flags, rc, props=None):
        if rc.is_failure:
            print(f"source refused connection: {reason_text(rc)}", file=sys.stderr)
            ready.set()
            return
        for f in args.filter:
            c.subscribe(f, qos=1)
        ready.set()

    def on_message(c, u, msg):
        if not msg.retain:
            return
        last_seen[0] = time.time()
        if msg.payload:
            found[msg.topic] = {"topic": msg.topic, "payload_b64": base64.b64encode(msg.payload).decode(),
                                "qos": msg.qos}

    client.on_connect = on_connect
    client.on_message = on_message
    connect(client, ep, args.source_protocol)
    client.loop_start()
    ready.wait(15)
    # Retained messages arrive right after SUBACK; stop when none arrived for --quiet seconds
    end = time.time() + args.max_wait
    while time.time() < end and time.time() - last_seen[0] < args.quiet:
        time.sleep(0.2)
    client.disconnect()
    client.loop_stop()
    items = sorted(found.values(), key=lambda x: x["topic"])
    sizes = [len(base64.b64decode(i["payload_b64"])) for i in items]
    print(f"collected {len(items)} retained messages"
          + (f", largest {max(sizes)} bytes, total {sum(sizes)} bytes" if sizes else ""), file=sys.stderr)
    return items


def publish(args, items: list[dict]) -> int:
    if args.limit and len(items) > args.limit:
        print(f"WARNING: {len(items)} retained messages but the plan allows {args.limit}; "
              f"filter or upgrade before copying", file=sys.stderr)
    if args.dry_run:
        for i in items:
            print(f"would publish retained {i['topic']} ({len(base64.b64decode(i['payload_b64']))} bytes)")
        return 0
    ep = parse_url(args.target, args.target_username, args.target_password)
    cid = f"{args.target_client_id}_retained-copy" if args.target_client_id else f"retained-copy-{uuid.uuid4().hex[:6]}"
    client = make_client(ep, cid, args.target_protocol)
    connected = threading.Event()
    client.on_connect = lambda c, u, f, rc, p=None: (print(f"target: {reason_text(rc)}", file=sys.stderr),
                                                     connected.set())
    connect(client, ep, args.target_protocol)
    client.loop_start()
    if not connected.wait(15):
        print("target connection timed out", file=sys.stderr)
        return 1
    failed = 0
    for n, i in enumerate(items, 1):
        topic = args.add_prefix + (i["topic"][len(args.strip_prefix):] if args.strip_prefix and
                                   i["topic"].startswith(args.strip_prefix) else i["topic"])
        info = client.publish(topic, base64.b64decode(i["payload_b64"]), qos=1, retain=True)
        try:
            info.wait_for_publish(10)
        except Exception as e:  # noqa: BLE001
            print(f"FAILED {topic}: {e}", file=sys.stderr)
            failed += 1
        if args.rate:
            time.sleep(1 / args.rate)
        if n % 100 == 0:
            print(f"{n}/{len(items)} published", file=sys.stderr)
    client.disconnect()
    client.loop_stop()
    print(f"published {len(items) - failed} retained messages, {failed} failed", file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source")
    ap.add_argument("--source-username", default=os.environ.get("SOURCE_USERNAME"))
    ap.add_argument("--source-password", default=os.environ.get("SOURCE_PASSWORD"))
    ap.add_argument("--source-client-id", default=f"retained-export-{uuid.uuid4().hex[:6]}")
    ap.add_argument("--source-cafile")
    ap.add_argument("--source-cert")
    ap.add_argument("--source-key")
    ap.add_argument("--source-protocol", type=int, choices=(4, 5), default=5)
    ap.add_argument("--filter", action="append", help="Source filter (repeatable, default '#')")
    ap.add_argument("--quiet", type=float, default=3, help="Stop after this many seconds without a retained message")
    ap.add_argument("--max-wait", type=float, default=120)
    ap.add_argument("--export", help="Write the collected messages to this JSON file")
    ap.add_argument("--import", dest="import_file", help="Read messages from a JSON file instead of a source broker")
    ap.add_argument("--target")
    ap.add_argument("--target-username", default=os.environ.get("CONNECTMQ_USERNAME"))
    ap.add_argument("--target-password", default=os.environ.get("CONNECTMQ_PASSWORD"))
    ap.add_argument("--target-client-id", default=os.environ.get("CONNECTMQ_CLIENT_ID"))
    ap.add_argument("--target-protocol", type=int, choices=(4, 5), default=5)
    ap.add_argument("--strip-prefix", default="")
    ap.add_argument("--add-prefix", default="")
    ap.add_argument("--rate", type=float, default=20, help="Max publishes per second (stay under the plan's rate)")
    ap.add_argument("--limit", type=int, default=0, help="Retained-message limit of the target plan, for a warning")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.filter = args.filter or ["#"]

    if args.import_file:
        with open(args.import_file, encoding="utf-8") as f:
            items = json.load(f)
    elif args.source:
        items = collect(args)
    else:
        ap.error("give --source or --import")

    if args.export:
        with open(args.export, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
        print(f"wrote {args.export}", file=sys.stderr)
    if args.target or args.dry_run:
        return publish(args, items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
