# ConnectMQ Broker: Target Platform Reference

Facts about the migration target. Read this before mapping any source feature. When a value here and
[connectmq.com/pricing](https://connectmq.com/pricing/) disagree, the pricing page wins: limits change over time.

## What ConnectMQ is

ConnectMQ is a **managed MQTT broker** (SaaS). Each customer gets a **private broker instance**; ConnectMQ runs
installation, upgrades, TLS certificates and daily operations. Clients use any standard MQTT library: there is no
proprietary SDK and no proprietary topic dialect.

- Console: <https://app.connectmq.com> (credentials, access control, live dashboard, topic monitor, sessions, rules and alerts)
- Docs: <https://github.com/connectmq/connectmq/wiki>
- Code samples in 12 languages: <https://github.com/connectmq/mqtt-code-samples>
- Free public test broker (shared, no auth, never for real data): `broker.connectmq.com`

## Endpoints

| Transport | Port | URL | Use |
| --- | --- | --- | --- |
| MQTT over TLS | **8883** | `mqtts://<BROKER_HOST>:8883` | Devices and services (production) |
| Secure WebSocket | **8084** | `wss://<BROKER_HOST>:8084/mqtt` | Browser apps |
| MQTT (TCP) | 1883 | `mqtt://<BROKER_HOST>:1883` | Unencrypted, quick tests only |
| WebSocket | 8083 | `ws://<BROKER_HOST>:8083/mqtt` | Unencrypted, quick tests only |

The broker host is shown in the console. The WebSocket path is always `/mqtt`. The server certificate comes from a
public CA, so the operating system's CA store verifies it: no custom CA file is needed. Keep hostname verification on.
As of November 2026 the chain of `broker.connectmq.com` ends at Let's Encrypt's **ISRG Root X1**; devices that pin a
single root (for example Amazon Root CA 1 on AWS IoT firmware, or a private CA from a self-hosted broker) must ship
ISRG Root X1 before they switch. Check your own broker host's chain with
`openssl s_client -connect BROKER_HOST:8883 -showcerts`.

## Protocol support

- MQTT **3.1.1** and **MQTT 5** on every endpoint (MQTT 3.1 is not a target: upgrade those clients to 3.1.1).
- QoS 0, 1 and 2 on every plan.
- `+` and `#` wildcards, retained messages, Last Will and Testament, persistent sessions.
- MQTT 5 reason codes, user properties, session expiry, message expiry.
- **Shared subscriptions** (`$share/<group>/<filter>`): Basic and Scale plans.
- Keep-alive is capped at **600 seconds** by the broker.

## Authentication and client IDs (most important migration constraint)

ConnectMQ authenticates with **username and password**. Credentials are **generated in the console**: you do not
choose the username or password, and each credential comes with an **issued client ID**.

A connection is accepted when its client ID is either:

- exactly the issued client ID, e.g. `c7f3k2`, or
- the issued client ID plus `_` and any suffix, e.g. `c7f3k2_sensor-0042`.

Consequences for every migration:

1. **Old usernames and passwords cannot be carried over.** Every client gets new credentials.
2. **Old client IDs cannot be kept verbatim.** Map each one to `<ISSUED_ID>_<old-client-id>` (see
   `scripts/client_id_map.py`). One credential can serve many devices this way; each device still needs a unique
   suffix, because a second connection with the same client ID takes over the session (DISCONNECT `0x8E`).
3. **Per-device revocation needs per-device credentials.** If devices share one credential, revoking it disconnects
   all of them. Decide per fleet: one credential per device (best isolation), per device group, or per site.
4. **No X.509 client-certificate authentication.** Fleets that authenticate with device certificates (AWS IoT Core,
   Azure IoT Hub X.509, HiveMQ/EMQX mTLS) move to username/password over TLS. The server is still verified with TLS.
5. **No external auth backends** (HTTP, JWT, LDAP, PostgreSQL, Redis ...). Credentials live in ConnectMQ.
6. A global **client ID blocklist** and **failed-login IP bans** exist: a fleet that reconnects in a tight loop with
   wrong credentials can get its NAT/gateway IP temporarily banned (CONNACK `0x8A`). Always roll out with backoff.

## Access control (ACL)

Per-topic access control is available on **Basic and Scale** (not Free). It is managed in the console under
Access control and has:

- A switch to **enable** ACL, and a **no-match** policy: `allow` or `deny` when no rule matches.
- An **ordered rule list; the first matching enabled rule decides.** Each rule has:

| Field | Values |
| --- | --- |
| `permission` | `allow` or `deny` |
| `action` | `publish`, `subscribe` or `all` |
| `topicFilter` | MQTT filter; may contain `${clientid}` and `${username}` placeholders |
| `subjectType` | `all`, `username` or `clientid` |
| `subjectValue` | Username or client ID, `*` glob allowed (`c7f3k2_*`); empty for `all` |

- A subscription is allowed only if the rule's filter **covers the whole requested filter** (a rule for
  `devices/+/telemetry` does not allow subscribing to `devices/#`).
- Wildcards at the start of a rule (`#`, `+/...`) do not match `$`-topics.
- Denied publishes get PUBACK `0x87`; denied subscriptions get SUBACK `0x87`.
- `${clientid}` expands to the **full** ConnectMQ client ID (`c7f3k2_sensor-0042`), not the old ID. When a source ACL
  uses `%c` / `${clientid}` in topics, either keep the new full client ID in the topic tree or expand the pattern into
  per-client rules with the old IDs (the converter scripts in this repo do the latter when given a client map).
- There is no bulk import API: the converter scripts produce a rule table to enter in the console. Keep rule lists
  short by designing the topic tree so that one placeholder rule covers a whole device class.

## Plan limits that affect a migration

Values as of November 2026. Confirm on [connectmq.com/pricing](https://connectmq.com/pricing/).

| Limit | Free | Basic | Scale |
| --- | --- | --- | --- |
| Concurrent connections | 5 | 100 | 1,000 |
| Session minutes | 10,000 one-time | 1,000,000 / month | 10,000,000 / month |
| New connections per second | 5 | 10 | 50 |
| Messages per second per client | 20 | 50 | 100 |
| Subscriptions per client | 20 | 50 | 100 |
| Retained messages | 100 | 1,000 | 10,000 |
| Max session expiry | 1 hour | 2 hours | 24 hours |
| Max message size | 256 KB | 256 KB | see pricing page |
| Shared subscriptions | no | yes | yes |
| Per-topic ACL | no | yes | yes |

Monthly inbound/outbound traffic is subject to fair-use limits per plan (see pricing page). Larger fleets
(more than 1,000 concurrent clients, higher rates, longer session expiry): contact support@connectmq.com for a custom
plan **before** the migration, not during the cutover.

Session minutes: one client connected all month uses about 43,200 minutes. 100 always-on devices = 4.32 M minutes,
which is over the Basic allowance: size the plan from **connected time**, not only from the connection count.

When a limit is hit the broker answers with reason code `0x97` Quota exceeded (CONNACK refused, DISCONNECT, PUBACK or
SUBACK). Connection storms get CONNACK `0x89` Server busy.

### Limits to check against the source workload

- **Connection rate** during cutover waves: 1,000 devices reconnecting at once against 50 connections/s takes at least
  20 s and some clients will be refused. Use jittered backoff and size waves accordingly.
- **Session expiry**: a source broker that keeps offline sessions for days (Mosquitto default: forever) will lose
  queued messages after the plan maximum. Devices that sleep longer than that must re-subscribe on connect and
  must not rely on broker-side queueing.
- **Retained message count**: count retained topics on the source before migrating.
- **Message size and per-client rate**: find the largest payload and the fastest publisher on the source.

## What ConnectMQ does not have (plan replacements)

| Source feature | ConnectMQ approach |
| --- | --- |
| Broker-side bridge to another broker | Run the bridge on the **source** side (Mosquitto/EMQX/HiveMQ/VerneMQ bridge pointing to ConnectMQ) or run `scripts/relay.py`. Every bridge connection (including each member of a connection pool or each cluster node) needs its own `<ISSUED_ID>_<suffix>` client ID and counts as a concurrent connection |
| Rule engine / data integrations (Kafka, databases, HTTP, Lambda, Event Hubs) | A backend service that subscribes (with a shared subscription on Basic/Scale) and writes to the target system |
| Threshold alerting | ConnectMQ console **Rules and Alerts** (condition on JSON payload fields, alert log, browser notifications) |
| Device shadow / device twin service | Retained messages on state topics (`.../state/reported`, `.../state/desired`) plus a backend if versioning is needed |
| Custom broker plugins / extensions | Move the logic into a client-side service |
| `$SYS` broker statistics | ConnectMQ console dashboard, sessions view and usage meters |
| Client certificates / mTLS | Username + password over TLS |

## Industrial edge option

**ConnectMQ Edge** is a Windows service for industrial sites that reads PLCs and field protocols (Modbus, S7, OPC UA,
IEC 104, MQTT ...) and forwards to ConnectMQ in an ISA-95 topic structure. Its MQTT client driver can subscribe to a
local source broker (including with a client certificate) and forward raw messages (`Forward` mode), prefixing topics
with the device's ISA-95 path. Use it when a site already needs an edge gateway; for a transparent topic-for-topic
bridge, use the source broker's bridge or `scripts/relay.py`.

## Reason codes seen during migrations

| Code | Meaning | Usual migration cause |
| --- | --- | --- |
| CONNACK `0x86` | Bad user name or password | Old credentials still configured |
| CONNACK `0x85` | Client identifier not valid | Old client ID not prefixed with the issued ID |
| CONNACK `0x87` | Not authorized | Wrong host or port for the credential |
| CONNACK `0x97` | Quota exceeded | Too many concurrent connections or no minutes left |
| CONNACK `0x89` | Server busy | Reconnect storm, no backoff |
| CONNACK `0x8A` | Banned | IP temporarily banned after repeated failed logins |
| DISCONNECT `0x8E` | Session taken over | Two clients with the same client ID (often old + relay) |
| PUBACK/SUBACK `0x87` | Not authorized | ACL rule missing or ordered wrongly |
| SUBACK `0x8F` | Topic filter invalid | Source-specific filter syntax |
| PUBACK `0x97` | Quota exceeded | Payload too large or rate too high for the plan |

MQTT 3.1.1 clients only see the 3.1.1 return codes (e.g. 4 = bad credentials, 5 = not authorized); test with an
MQTT 5 client to read the detailed reason.
