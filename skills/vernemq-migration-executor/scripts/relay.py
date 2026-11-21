#!/usr/bin/env python3
"""MQTT relay (bridge) between a source broker and ConnectMQ for the parallel-run phase of a migration.

Use it when the source broker cannot bridge to ConnectMQ itself (AWS IoT Core, HiveMQ Cloud, managed EMQX without
bridge access) or when you want the bridge outside both brokers.

  --forward FILTER   source -> ConnectMQ (repeatable), e.g. telemetry published by devices still on the source
  --reverse FILTER   ConnectMQ -> source (repeatable), e.g. commands for devices still on the source

Loop prevention: forward and reverse filters must not overlap (checked at start-up). With MQTT 5 on both sides the
relay also tags every message it publishes with the user property x-relay=<relay id> and drops messages that
already carry it, and subscribes with no-local.

Topic mapping: --strip-prefix removes a prefix from source topics before publishing to ConnectMQ and --add-prefix
adds one (the reverse direction undoes both).

Credentials: SOURCE_USERNAME / SOURCE_PASSWORD and CONNECTMQ_USERNAME / CONNECTMQ_PASSWORD / CONNECTMQ_CLIENT_ID.
The relay uses one connection on each broker (client IDs <id>_relay on ConnectMQ, --source-client-id on the source)
with persistent sessions so short restarts do not lose QoS 1/2 messages.

Examples:
  # Mosquitto/EMQX/HiveMQ/VerneMQ -> ConnectMQ, telemetry only
  python relay.py --source mqtt://old-broker:1883 --target mqtts://YOUR_BROKER_HOST:8883 \\
      --forward 'devices/+/telemetry' --forward 'devices/+/status'

  # AWS IoT Core (X.509) <-> ConnectMQ, commands flow back to devices not yet migrated
  python relay.py --source mqtts://xxxx-ats.iot.eu-west-1.amazonaws.com:8883 --source-client-id migration-relay \\
      --source-cafile AmazonRootCA1.pem --source-cert relay.cert.pem --source-key relay.private.key \\
      --target mqtts://YOUR_BROKER_HOST:8883 --forward 'dt/#' --reverse 'cmd/#'

Requires paho-mqtt 2.x.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import uuid
from collections import Counter

from mqtt_common import (PacketTypes, Properties, connect, filters_overlap, make_client, mqtt, parse_url,
                         reason_text)

try:
    from paho.mqtt.subscribeoptions import SubscribeOptions
except ImportError:  # pragma: no cover
    SubscribeOptions = None

RELAY_PROPERTY = "x-relay"


def map_topic(topic: str, strip: str, add: str, reverse: bool) -> str | None:
    if not reverse:
        if strip:
            if not topic.startswith(strip):
                return topic
            topic = topic[len(strip):]
        return add + topic
    if add:
        if not topic.startswith(add):
            return None
        topic = topic[len(add):]
    return strip + topic


def map_filter(filt: str, strip: str, add: str, reverse: bool) -> str:
    """Filter on the side we subscribe to. Forward filters are source filters; reverse filters are given as SOURCE
    topics too, so they are mapped into ConnectMQ topics before subscribing there."""
    if not reverse:
        return filt
    if strip and filt.startswith(strip):
        filt = filt[len(strip):]
    return add + filt


class Side:
    def __init__(self, name, ep, client_id, protocol, cafile=None, cert=None, key=None, insecure=False):
        self.name, self.ep, self.client_id, self.protocol = name, ep, client_id, protocol
        self.client = make_client(ep, client_id, protocol, clean_start=False, cafile=cafile, certfile=cert,
                                  keyfile=key, insecure=insecure)
        self.client.max_queued_messages_set(100_000)
        self.client.max_inflight_messages_set(50)
        self.connected = threading.Event()
        self.filters: list[tuple[str, int]] = []
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc.is_failure:
            print(f"[{self.name}] connect refused: {reason_text(rc)}", flush=True)
            return
        print(f"[{self.name}] connected to {self.ep.display()} as {self.client_id} "
              f"(session present: {getattr(flags, 'session_present', '?')})", flush=True)
        self.connected.set()
        for filt, qos in self.filters:
            if self.protocol == 5 and SubscribeOptions:
                opts = SubscribeOptions(qos=qos, noLocal=True, retainAsPublished=True)
                client.subscribe(filt, options=opts)
            else:
                client.subscribe(filt, qos=qos)
            print(f"[{self.name}] subscribed {filt} (qos {qos})", flush=True)

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        self.connected.clear()
        print(f"[{self.name}] disconnected: {reason_text(rc)}; reconnecting with backoff", flush=True)

    def start(self, session_expiry):
        connect(self.client, self.ep, self.protocol, clean_start=False, session_expiry=session_expiry)
        self.client.loop_start()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="Source broker URL")
    ap.add_argument("--source-username", default=os.environ.get("SOURCE_USERNAME"))
    ap.add_argument("--source-password", default=os.environ.get("SOURCE_PASSWORD"))
    ap.add_argument("--source-client-id", default=f"connectmq-relay-{uuid.uuid4().hex[:6]}",
                    help="Fixed ID recommended so the source keeps the relay's session")
    ap.add_argument("--source-cafile")
    ap.add_argument("--source-cert", help="Client certificate (AWS IoT Core, mTLS sources)")
    ap.add_argument("--source-key")
    ap.add_argument("--source-insecure", action="store_true", help="Skip source TLS verification (tests only)")
    ap.add_argument("--source-protocol", type=int, choices=(4, 5), default=5)
    ap.add_argument("--target", required=True, help="ConnectMQ URL, e.g. mqtts://YOUR_BROKER_HOST:8883")
    ap.add_argument("--target-username", default=os.environ.get("CONNECTMQ_USERNAME"))
    ap.add_argument("--target-password", default=os.environ.get("CONNECTMQ_PASSWORD"))
    ap.add_argument("--target-client-id", default=os.environ.get("CONNECTMQ_CLIENT_ID"),
                    help="Issued client ID; the relay connects as <ID>_relay")
    ap.add_argument("--target-protocol", type=int, choices=(4, 5), default=5)
    ap.add_argument("--forward", action="append", default=[], help="Source filter relayed to ConnectMQ")
    ap.add_argument("--reverse", action="append", default=[], help="Source-side filter relayed back from ConnectMQ")
    ap.add_argument("--qos", type=int, choices=(0, 1, 2), default=1)
    ap.add_argument("--strip-prefix", default="")
    ap.add_argument("--add-prefix", default="")
    ap.add_argument("--no-retain", action="store_true", help="Publish everything with retain=false")
    ap.add_argument("--session-expiry", type=int, default=3600,
                    help="Session expiry (s) on MQTT 5; ConnectMQ caps it per plan")
    ap.add_argument("--stats-interval", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="Subscribe and count, but do not publish")
    args = ap.parse_args()

    if not args.forward and not args.reverse:
        ap.error("give at least one --forward or --reverse filter")
    for f in args.forward:
        for r in args.reverse:
            if filters_overlap(f, r):
                ap.error(f"--forward {f} overlaps --reverse {r}: messages would loop between the brokers")

    relay_id = uuid.uuid4().hex[:8]
    src = Side("source", parse_url(args.source, args.source_username, args.source_password), args.source_client_id,
               args.source_protocol, args.source_cafile, args.source_cert, args.source_key, args.source_insecure)
    tgt_id = f"{args.target_client_id}_relay" if args.target_client_id else f"relay-{relay_id}"
    tgt = Side("connectmq", parse_url(args.target, args.target_username, args.target_password), tgt_id,
               args.target_protocol)

    src.filters = [(f, args.qos) for f in args.forward]
    tgt.filters = [(map_filter(f, args.strip_prefix, args.add_prefix, True), args.qos) for f in args.reverse]

    stats = Counter()
    lock = threading.Lock()

    def make_handler(dest: Side, reverse: bool, direction: str):
        def on_message(client, userdata, msg):
            props = getattr(msg, "properties", None)
            user_props = dict(getattr(props, "UserProperty", []) or []) if props else {}
            if RELAY_PROPERTY in user_props:
                with lock:
                    stats[f"{direction} skipped (already relayed)"] += 1
                return
            topic = map_topic(msg.topic, args.strip_prefix, args.add_prefix, reverse)
            if topic is None:
                return
            with lock:
                stats[f"{direction} received"] += 1
            if args.dry_run:
                return
            out_props = None
            if dest.protocol == 5:
                out_props = Properties(PacketTypes.PUBLISH)
                out_props.UserProperty = [(k, v) for k, v in user_props.items()] + [(RELAY_PROPERTY, relay_id)]
                for attr in ("ContentType", "ResponseTopic", "CorrelationData", "MessageExpiryInterval",
                             "PayloadFormatIndicator"):
                    if props is not None and hasattr(props, attr):
                        setattr(out_props, attr, getattr(props, attr))
            info = dest.client.publish(topic, msg.payload, qos=min(msg.qos, args.qos),
                                       retain=msg.retain and not args.no_retain, properties=out_props)
            with lock:
                stats[f"{direction} published" if info.rc == mqtt.MQTT_ERR_SUCCESS
                      else f"{direction} queued/failed rc={info.rc}"] += 1
        return on_message

    src.client.on_message = make_handler(tgt, False, "source->connectmq")
    tgt.client.on_message = make_handler(src, True, "connectmq->source")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    print(f"relay {relay_id}: forward {args.forward or '-'}, reverse {args.reverse or '-'}, qos {args.qos}"
          f"{', DRY RUN' if args.dry_run else ''}", flush=True)
    tgt.start(args.session_expiry)
    src.start(args.session_expiry)

    last = time.time()
    while not stop.wait(1):
        if time.time() - last >= args.stats_interval:
            last = time.time()
            with lock:
                line = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())) or "no messages yet"
            print(f"[stats] {line}", flush=True)

    print("stopping relay", flush=True)
    for side in (src, tgt):
        side.client.disconnect()
        side.client.loop_stop()
    with lock:
        print("[final] " + (", ".join(f"{k}: {v}" for k, v in sorted(stats.items())) or "no messages"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
