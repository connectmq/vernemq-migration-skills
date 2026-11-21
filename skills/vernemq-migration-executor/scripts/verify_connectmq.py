#!/usr/bin/env python3
"""Smoke-tests a ConnectMQ broker (or any MQTT broker) before and during a migration.

Checks: TLS connect, subscribe/publish at QoS 0/1/2, retained messages, Last Will, persistent session queueing,
and (optionally) ACL expectations. Prints a PASS/FAIL table and exits 1 on any failure.

Credentials come from flags or the environment: CONNECTMQ_USERNAME, CONNECTMQ_PASSWORD, CONNECTMQ_CLIENT_ID.
The script opens up to three connections at once (<ID>_verify-a, _verify-b, _verify-w): keep a free connection
slot on small plans.

Examples:
  python verify_connectmq.py --url mqtts://YOUR_BROKER_HOST:8883 --client-id c7f3k2
  python verify_connectmq.py --url mqtts://YOUR_BROKER_HOST:8883 --prefix factory/line1/migration-test
  python verify_connectmq.py --url mqtts://YOUR_BROKER_HOST:8883 --expect-deny-publish 'secret/x'
  python verify_connectmq.py --url mqtt://broker.connectmq.com:1883 --client-id my-test   # public broker, no auth

Requires paho-mqtt 2.x.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid

from mqtt_common import connect, make_client, mqtt, parse_url, reason_text


class Probe:
    """One connection that records connect results, messages and acks."""

    def __init__(self, args, suffix: str, clean_start=True, session_expiry=0, will=None, protocol=5):
        self.args = args
        self.client_id = f"{args.client_id}_{suffix}" if args.client_id else f"verify-{uuid.uuid4().hex[:8]}-{suffix}"
        self.ep = parse_url(args.url, args.username, args.password)
        self.protocol = protocol
        self.client = make_client(self.ep, self.client_id, protocol, clean_start, cafile=args.cafile)
        self.connected = threading.Event()
        self.connack = None
        self.session_present = None
        self.messages: list[mqtt.MQTTMessage] = []
        self.msg_event = threading.Condition()
        self.subacks: dict[int, list] = {}
        self.pubacks: dict[int, object] = {}
        self.clean_start = clean_start
        self.session_expiry = session_expiry
        if will:
            topic, payload = will
            self.client.will_set(topic, payload, qos=1, retain=False)
        c = self.client
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_subscribe = self._on_subscribe
        c.on_publish = self._on_publish

    def _on_connect(self, client, userdata, flags, rc, props=None):
        self.connack = rc
        self.session_present = getattr(flags, "session_present", None)
        self.connected.set()

    def _on_message(self, client, userdata, msg):
        with self.msg_event:
            self.messages.append(msg)
            self.msg_event.notify_all()

    def _on_subscribe(self, client, userdata, mid, reason_codes, props=None):
        self.subacks[mid] = list(reason_codes)

    def _on_publish(self, client, userdata, mid, rc, props=None):
        self.pubacks[mid] = rc

    def start(self, timeout=10) -> bool:
        connect(self.client, self.ep, self.protocol, self.clean_start, self.session_expiry)
        self.client.loop_start()
        if not self.connected.wait(timeout):
            return False
        return not self.connack.is_failure

    def subscribe(self, topic, qos=1, timeout=5):
        rc, mid = self.client.subscribe(topic, qos=qos)
        end = time.time() + timeout
        while mid not in self.subacks and time.time() < end:
            time.sleep(0.05)
        return self.subacks.get(mid)

    def publish(self, topic, payload, qos=1, retain=False, timeout=5):
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        if qos == 0:
            return True, None
        end = time.time() + timeout
        while info.mid not in self.pubacks and time.time() < end:
            time.sleep(0.05)
        rc = self.pubacks.get(info.mid)
        return (rc is not None and not getattr(rc, "is_failure", False)), rc

    def wait_for(self, predicate, timeout=5):
        end = time.time() + timeout
        with self.msg_event:
            while True:
                for m in self.messages:
                    if predicate(m):
                        return m
                left = end - time.time()
                if left <= 0:
                    return None
                self.msg_event.wait(left)

    def stop(self, graceful=True):
        if graceful:
            self.client.disconnect()
        else:
            # Simulate a network failure: close the socket without sending DISCONNECT, so the broker sends the Will
            sock = self.client.socket()
            self.client.loop_stop()
            if sock:
                sock.close()
            return
        self.client.loop_stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="mqtts://HOST:8883 or wss://HOST:8084/mqtt")
    ap.add_argument("--username", default=os.environ.get("CONNECTMQ_USERNAME"))
    ap.add_argument("--password", default=os.environ.get("CONNECTMQ_PASSWORD"))
    ap.add_argument("--client-id", default=os.environ.get("CONNECTMQ_CLIENT_ID"),
                    help="Issued client ID; test connections use <ID>_verify-a etc.")
    ap.add_argument("--prefix", default=None, help="Topic prefix for test messages (default migration-check/<run>)")
    ap.add_argument("--protocol", type=int, choices=(4, 5), default=5, help="5 = MQTT 5 (default), 4 = MQTT 3.1.1")
    ap.add_argument("--cafile", help="Custom CA file (not needed for ConnectMQ)")
    ap.add_argument("--session-expiry", type=int, default=300, help="Session expiry used for the persistence test")
    ap.add_argument("--expect-deny-publish", action="append", default=[], help="Topic that ACL must refuse (repeatable)")
    ap.add_argument("--expect-deny-subscribe", action="append", default=[], help="Filter ACL must refuse (repeatable)")
    ap.add_argument("--skip", action="append", default=[], choices=["retained", "will", "session", "qos2"])
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    prefix = (args.prefix or "migration-check").rstrip("/") + f"/{run}"
    results: list[tuple[str, bool, str]] = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)

    a = Probe(args, "verify-a", protocol=args.protocol)
    t0 = time.time()
    ok = a.start()
    record("connect", ok, f"{a.ep.display()} as {a.client_id}: {reason_text(a.connack) if a.connack else 'timeout'} "
                          f"in {time.time() - t0:.2f}s")
    if not ok:
        return report(results)

    codes = a.subscribe(prefix + "/#", qos=2)
    granted = codes[0] if codes else None
    record("subscribe qos2", bool(codes) and not granted.is_failure, f"granted {reason_text(granted)}")

    for qos in (0, 1, 2):
        if qos == 2 and "qos2" in args.skip:
            continue
        topic = f"{prefix}/qos{qos}"
        payload = f"qos{qos}-{run}"
        t0 = time.time()
        sent, rc = a.publish(topic, payload, qos=qos)
        got = a.wait_for(lambda m, t=topic: m.topic == t)
        record(f"publish/receive qos{qos}", sent and got is not None,
               f"ack {reason_text(rc) if rc is not None else '-'}, round trip "
               f"{(time.time() - t0) * 1000:.0f} ms" if got else f"ack {reason_text(rc) if rc else 'none'}, no message")

    if "retained" not in args.skip:
        topic = f"{prefix}/retained"
        a.publish(topic, f"retained-{run}", qos=1, retain=True)
        b = Probe(args, "verify-b", protocol=args.protocol)
        if b.start():
            b.subscribe(topic, qos=1)
            got = b.wait_for(lambda m: m.topic == topic and m.retain)
            record("retained message", got is not None, "delivered to a new subscriber" if got else "not delivered")
            b.stop()
        else:
            record("retained message", False, f"second connection refused: {reason_text(b.connack)}")
        a.publish(topic, b"", qos=1, retain=True)  # clear it

    if "will" not in args.skip:
        will_topic = f"{prefix}/will"
        a.subscribe(will_topic, qos=1)
        w = Probe(args, "verify-w", will=(will_topic, f"offline-{run}"), protocol=args.protocol)
        if w.start():
            time.sleep(0.5)
            w.stop(graceful=False)
            got = a.wait_for(lambda m: m.topic == will_topic, timeout=15)
            record("last will", got is not None, "published after abrupt disconnect" if got else
                   "not received within 15 s (broker may wait for keep-alive)")
        else:
            record("last will", False, f"connection refused: {reason_text(w.connack)}")

    if "session" not in args.skip and args.protocol == 5:
        topic = f"{prefix}/session"
        s = Probe(args, "verify-s", clean_start=True, session_expiry=args.session_expiry)
        if s.start():
            s.subscribe(topic, qos=1)
            s.stop()
            time.sleep(0.5)
            a.publish(topic, f"queued-{run}", qos=1)
            time.sleep(0.5)
            s2 = Probe(args, "verify-s", clean_start=False, session_expiry=args.session_expiry)
            s2.start()
            got = s2.wait_for(lambda m: m.topic == topic, timeout=5)
            record("persistent session", bool(s2.session_present) and got is not None,
                   f"session_present={s2.session_present}, queued message {'delivered' if got else 'missing'}")
            s2.client.disconnect()  # leaves the session; it expires after --session-expiry
            s2.client.loop_stop()
        else:
            record("persistent session", False, f"connection refused: {reason_text(s.connack)}")

    for topic in args.expect_deny_publish:
        sent, rc = a.publish(topic, "acl-test", qos=1)
        record(f"acl deny publish {topic}", not sent and rc is not None, f"ack {reason_text(rc) if rc else 'none'}")
    for filt in args.expect_deny_subscribe:
        codes = a.subscribe(filt, qos=1)
        record(f"acl deny subscribe {filt}", bool(codes) and codes[0].is_failure,
               f"suback {reason_text(codes[0]) if codes else 'none'}")

    a.stop()
    return report(results)


def report(results) -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
