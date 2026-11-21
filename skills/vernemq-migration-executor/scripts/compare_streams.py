#!/usr/bin/env python3
"""Subscribes to the same filters on the source broker and on ConnectMQ and compares what arrives.

Use it after the bridge/relay is up and after every client wave: it shows topics that only reach one side, message
count differences per topic, and payload mismatches. Exit code 1 when the difference exceeds --tolerance.

  python compare_streams.py --source mqtt://old-broker:1883 --target mqtts://YOUR_BROKER_HOST:8883 \\
      --filter 'devices/#' --duration 120

Credentials: SOURCE_USERNAME / SOURCE_PASSWORD, CONNECTMQ_USERNAME / CONNECTMQ_PASSWORD / CONNECTMQ_CLIENT_ID.
Requires paho-mqtt 2.x.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid
from collections import Counter, defaultdict

from mqtt_common import connect, make_client, parse_url, reason_text


def start(name, ep, client_id, protocol, filters, counts, hashes, cafile=None, cert=None, key=None,
          strip="", add=""):
    client = make_client(ep, client_id, protocol, cafile=cafile, certfile=cert, keyfile=key)

    def on_connect(c, u, flags, rc, props=None):
        print(f"[{name}] {reason_text(rc)}", file=sys.stderr)
        for f in filters:
            c.subscribe(f, qos=1)

    def on_message(c, u, msg):
        if msg.retain:
            return  # only compare live traffic
        topic = msg.topic
        if name == "source":
            if strip and topic.startswith(strip):
                topic = topic[len(strip):]
            topic = add + topic
        counts[topic] += 1
        hashes[topic].add(hashlib.sha1(msg.payload).hexdigest())

    client.on_connect = on_connect
    client.on_message = on_message
    connect(client, ep, protocol)
    client.loop_start()
    return client


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--source-username", default=os.environ.get("SOURCE_USERNAME"))
    ap.add_argument("--source-password", default=os.environ.get("SOURCE_PASSWORD"))
    ap.add_argument("--source-cafile")
    ap.add_argument("--source-cert")
    ap.add_argument("--source-key")
    ap.add_argument("--source-protocol", type=int, choices=(4, 5), default=5)
    ap.add_argument("--source-client-id", help="Client ID on the source (default compare-<random>); needed when the "
                                               "source policy pins client IDs, e.g. AWS IoT iot:Connect")
    ap.add_argument("--target", required=True)
    ap.add_argument("--target-username", default=os.environ.get("CONNECTMQ_USERNAME"))
    ap.add_argument("--target-password", default=os.environ.get("CONNECTMQ_PASSWORD"))
    ap.add_argument("--target-client-id", default=os.environ.get("CONNECTMQ_CLIENT_ID"))
    ap.add_argument("--filter", action="append", required=True)
    ap.add_argument("--strip-prefix", default="", help="Same topic mapping as the relay/bridge")
    ap.add_argument("--add-prefix", default="")
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--tolerance", type=float, default=0.01, help="Allowed relative count difference (default 1%%)")
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    sc, sh = Counter(), defaultdict(set)
    tc, th = Counter(), defaultdict(set)
    s = start("source", parse_url(args.source, args.source_username, args.source_password),
              args.source_client_id or f"compare-{run}",
              args.source_protocol, args.filter, sc, sh, args.source_cafile, args.source_cert, args.source_key,
              args.strip_prefix, args.add_prefix)
    tgt_filters = [args.add_prefix + (f[len(args.strip_prefix):] if args.strip_prefix and
                                      f.startswith(args.strip_prefix) else f) for f in args.filter]
    t = start("connectmq", parse_url(args.target, args.target_username, args.target_password),
              f"{args.target_client_id}_compare" if args.target_client_id else f"compare-{run}-t", 5,
              tgt_filters, tc, th)
    print(f"comparing for {args.duration}s ...", file=sys.stderr)
    time.sleep(args.duration)
    for c in (s, t):
        c.disconnect()
        c.loop_stop()

    topics = sorted(set(sc) | set(tc))
    only_src = [x for x in topics if x not in tc]
    only_tgt = [x for x in topics if x not in sc]
    diff = [(x, sc[x], tc[x]) for x in topics if x in sc and x in tc and sc[x] != tc[x]]
    payload_mismatch = [x for x in topics if x in sh and x in th and sh[x] != th[x]]

    total_s, total_t = sum(sc.values()), sum(tc.values())
    print(f"\nsource: {total_s} messages on {len(sc)} topics; connectmq: {total_t} messages on {len(tc)} topics")
    if only_src:
        print(f"\nonly on source ({len(only_src)}):\n  " + "\n  ".join(only_src[:50]))
    if only_tgt:
        print(f"\nonly on connectmq ({len(only_tgt)}):\n  " + "\n  ".join(only_tgt[:50]))
    if diff:
        print(f"\ncount differences ({len(diff)}):")
        for x, a, b in diff[:50]:
            print(f"  {x}: source {a}, connectmq {b}")
    if payload_mismatch:
        print(f"\npayload sets differ ({len(payload_mismatch)}), e.g. {payload_mismatch[:5]}")

    rel = abs(total_s - total_t) / max(total_s, 1)
    ok = rel <= args.tolerance and not only_src and not only_tgt
    print(f"\n{'MATCH' if ok else 'MISMATCH'}: relative difference {rel:.2%}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
