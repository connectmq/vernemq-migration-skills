# VerneMQ Bridge to ConnectMQ

ConnectMQ does not open bridges itself, so the bridge runs on VerneMQ with the `vmq_bridge` plugin. It is one MQTT
client connection to ConnectMQ and counts toward the plan's concurrent connections. Configure it on **one node** of
the cluster: VerneMQ routes messages between nodes, so that node's bridge sees the whole cluster's traffic, and a
second bridge with the same client ID would keep taking over the first one's session.

Keys below follow the VerneMQ bridge documentation (<https://docs.vernemq.com/configuring-vernemq/bridge>); check it
for your version before applying them. The plugin bridges with MQTT 3.1.1: MQTT 5 messages are forwarded, but user
properties are dropped.

## Configuration (`vernemq.conf`)

```conf
plugins.vmq_bridge = on

# TLS bridge named br0 to ConnectMQ
vmq_bridge.ssl.br0 = BROKER_HOST:8883

# Verify ConnectMQ's public certificate with the system CA bundle
vmq_bridge.ssl.br0.cafile = /etc/ssl/certs/ca-certificates.crt
vmq_bridge.ssl.br0.insecure = off

# Credentials issued in the ConnectMQ console
vmq_bridge.ssl.br0.username = CONNECTMQ_USERNAME
vmq_bridge.ssl.br0.password = CONNECTMQ_PASSWORD
# Must be the issued client ID or <ISSUED_ID>_<suffix>
vmq_bridge.ssl.br0.client_id = ISSUED_ID_bridge

# ConnectMQ is not a VerneMQ/Mosquitto bridge peer
vmq_bridge.ssl.br0.try_private = off
# Keep the session so QoS 1 messages survive short reconnects
vmq_bridge.ssl.br0.cleansession = off
vmq_bridge.ssl.br0.keepalive_interval = 60
vmq_bridge.ssl.br0.restart_timeout = 10
# Messages buffered while ConnectMQ is unreachable (0 disables, overflow is dropped)
vmq_bridge.ssl.br0.max_outgoing_buffered_messages = 1000

# Forward: VerneMQ -> ConnectMQ (what devices on VerneMQ publish)
vmq_bridge.ssl.br0.topic.1 = devices/+/telemetry out 1
vmq_bridge.ssl.br0.topic.2 = devices/+/status out 1
# Reverse: ConnectMQ -> VerneMQ (commands for devices still on VerneMQ)
# '#' starts a comment in vernemq.conf: use '*' for the multi-level wildcard in bridge topics
vmq_bridge.ssl.br0.topic.3 = devices/+/commands/* in 1
```

The CA path depends on the image: Debian/Ubuntu `/etc/ssl/certs/ca-certificates.crt`, Alpine and RHEL-based images
have their own bundle path (`/etc/ssl/cert.pem`, `/etc/pki/tls/certs/ca-bundle.crt`).

Topic lines use the Mosquitto bridge format: `<pattern> [in|out|both] [qos] [retain] [<local prefix> <remote prefix>]`.

- **Write `*` instead of `#`** in bridge topic patterns: the config parser treats `#` as the start of a comment, so
  `devices/#` silently becomes `devices/`. In `vmq.acl` and in MQTT clients `#` is still `#`.
- Direction defaults to `out` and QoS to 0, so always write both.
- Incoming (`in`) messages are republished **without** the retain flag unless the line has `retain`
  (e.g. `devices/+/config in 1 retain`). Add it for retained state topics coming from ConnectMQ.

- `out` = VerneMQ to ConnectMQ, `in` = ConnectMQ to VerneMQ.
- Avoid `both` during a migration: once some devices publish on ConnectMQ and others on VerneMQ, `both` sends each
  message in both directions and loops or duplicates are hard to rule out.
- Prefixes rewrite topics. Use the remote prefix to give a mountpoint's traffic its own branch on ConnectMQ.

### Docker / Kubernetes

The official image maps environment variables to `vernemq.conf` keys: `DOCKER_VERNEMQ_` prefix, double underscore
for a dot. For example:

```yaml
DOCKER_VERNEMQ_PLUGINS__VMQ_BRIDGE: "on"
DOCKER_VERNEMQ_VMQ_BRIDGE__SSL__BR0: "BROKER_HOST:8883"
DOCKER_VERNEMQ_VMQ_BRIDGE__SSL__BR0__CLIENT_ID: "ISSUED_ID_bridge"
DOCKER_VERNEMQ_VMQ_BRIDGE__SSL__BR0__TOPIC__1: "devices/+/telemetry out 1"
```

Put the password in a Kubernetes secret mounted as an environment variable. Because Helm values usually apply to
every pod of the StatefulSet, prefer `relay.py` (below) or a dedicated bridge node over adding bridge variables to
the whole cluster.

## Mountpoints

Messages of clients on a mountpoint live in that mountpoint's topic space. Make sure the bridge sees the topics you
expect (test with `compare_streams.py` connected through the same listener as the devices), and give each mountpoint
a ConnectMQ prefix if several mountpoints use the same topic names. If the plugin cannot reach a mountpoint the way
you need, run one `relay.py` per mountpoint, each connected to that mountpoint's listener, with `--add-prefix`.

## ACL rules for the bridge on ConnectMQ

Add these before enabling ACL (replace `ISSUED_ID_bridge` with the bridge's client ID and add prefixes if used):

| Permission | Action | Topic filter | Subject | Value |
| --- | --- | --- | --- | --- |
| allow | publish | `devices/+/telemetry` | clientid | `ISSUED_ID_bridge` |
| allow | publish | `devices/+/status` | clientid | `ISSUED_ID_bridge` |
| allow | subscribe | `devices/+/commands/#` | clientid | `ISSUED_ID_bridge` |

## Checking the bridge

```bash
vmq-admin plugin show            # vmq_bridge listed and started
vmq-admin bridge show            # bridge br0 and its state
python scripts/compare_streams.py --source mqtt://VERNEMQ_HOST:1883 --target mqtts://BROKER_HOST:8883 \
  --filter 'devices/#' --duration 300
```

## When the plugin cannot be used

Run the external relay instead (same topics, same loop rules). It connects to VerneMQ as an ordinary client:

```bash
python scripts/relay.py --source mqtt://VERNEMQ_HOST:1883 --source-client-id connectmq-relay \
  --target mqtts://BROKER_HOST:8883 \
  --forward 'devices/+/telemetry' --forward 'devices/+/status' --reverse 'devices/+/commands/#'
```

Give the relay a VerneMQ user whose ACL allows reading the forward topics and writing the reverse topics. Run it
under a supervisor (systemd, a Kubernetes Deployment with one replica); it prints counters every 30 s.

## Removing the bridge at cutover

1. Confirm no device still publishes forward topics on VerneMQ (`compare_streams.py` shows no source traffic and
   `vmq-admin session show --is_online` lists only the bridge/relay).
2. Remove the `vmq_bridge.*` settings, `vmq-admin plugin disable --name vmq_bridge` (or stop the relay).
3. Remove the bridge ACL rules and credential on ConnectMQ.
