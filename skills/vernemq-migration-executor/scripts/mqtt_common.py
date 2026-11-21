"""Connection helpers shared by verify_connectmq.py, relay.py, copy_retained.py and compare_streams.py.

Requires paho-mqtt 2.x:  pip install "paho-mqtt>=2.0"

Broker URLs: mqtt://host:1883, mqtts://host:8883, ws://host:8083/mqtt, wss://host:8084/mqtt
Credentials can be embedded (mqtts://user:pass@host:8883) but environment variables are safer; see each script.
"""

from __future__ import annotations

import ssl
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
    from paho.mqtt.properties import Properties
    from paho.mqtt.packettypes import PacketTypes
except ImportError:  # pragma: no cover
    sys.exit('paho-mqtt 2.x is required: pip install "paho-mqtt>=2.0"')

DEFAULT_PORTS = {"mqtt": 1883, "mqtts": 8883, "ws": 8083, "wss": 8084}


@dataclass
class Endpoint:
    scheme: str
    host: str
    port: int
    path: str
    username: str | None
    password: str | None

    @property
    def tls(self) -> bool:
        return self.scheme in ("mqtts", "wss")

    @property
    def websocket(self) -> bool:
        return self.scheme in ("ws", "wss")

    def display(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.path if self.websocket else ''}"


def parse_url(url: str, username: str | None = None, password: str | None = None) -> Endpoint:
    if "://" not in url:
        url = "mqtts://" + url
    u = urlparse(url)
    if u.scheme not in DEFAULT_PORTS:
        raise SystemExit(f"unsupported scheme in {url}; use mqtt, mqtts, ws or wss")
    return Endpoint(
        scheme=u.scheme,
        host=u.hostname or "",
        port=u.port or DEFAULT_PORTS[u.scheme],
        path=u.path or "/mqtt",
        username=username if username is not None else (unquote(u.username) if u.username else None),
        password=password if password is not None else (unquote(u.password) if u.password else None),
    )


def make_client(ep: Endpoint, client_id: str, protocol: int = 5, clean_start: bool = True,
                cafile: str | None = None, certfile: str | None = None, keyfile: str | None = None,
                insecure: bool = False) -> "mqtt.Client":
    proto = mqtt.MQTTv5 if protocol == 5 else mqtt.MQTTv311
    kwargs = dict(callback_api_version=CallbackAPIVersion.VERSION2, client_id=client_id, protocol=proto,
                  transport="websockets" if ep.websocket else "tcp")
    if proto == mqtt.MQTTv311:
        kwargs["clean_session"] = clean_start
    client = mqtt.Client(**kwargs)
    if ep.websocket:
        client.ws_set_options(path=ep.path)
    if ep.tls:
        ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        if certfile:
            ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ctx)
    if ep.username is not None:
        client.username_pw_set(ep.username, ep.password)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def connect(client: "mqtt.Client", ep: Endpoint, protocol: int = 5, clean_start: bool = True,
            session_expiry: int = 0, keepalive: int = 60) -> None:
    if protocol == 5:
        props = Properties(PacketTypes.CONNECT)
        if session_expiry:
            props.SessionExpiryInterval = session_expiry
        client.connect(ep.host, ep.port, keepalive=keepalive, clean_start=clean_start, properties=props)
    else:
        client.connect(ep.host, ep.port, keepalive=keepalive)


def reason_text(rc) -> str:
    try:
        return f"{rc} (0x{int(rc.value):02X})" if hasattr(rc, "value") else str(rc)
    except Exception:  # pragma: no cover
        return str(rc)


def filters_overlap(a: str, b: str) -> bool:
    """True when some topic could match both filters."""
    pa, pb = a.split("/"), b.split("/")
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else None
        y = pb[i] if i < len(pb) else None
        if x == "#" or y == "#":
            return True
        if x is None or y is None:
            return False
        if x == "+" or y == "+" or x == y:
            continue
        return False
    return True


def topic_matches(filter_: str, topic: str) -> bool:
    return mqtt.topic_matches_sub(filter_, topic)
