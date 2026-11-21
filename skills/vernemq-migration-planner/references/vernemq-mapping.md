# VerneMQ to ConnectMQ: Feature and Configuration Mapping

## Configuration mapping (`vernemq.conf`)

| VerneMQ | ConnectMQ | Notes |
| --- | --- | --- |
| `listener.tcp.<name> = IP:1883` | `mqtt://HOST:1883` | Tests only; production uses 8883 |
| `listener.ssl.<name> = IP:8883` + `listener.ssl.cafile/certfile/keyfile` | `mqtts://HOST:8883` | Public CA: clients use the OS CA store |
| `listener.ws.<name>` / `listener.wss.<name>` | `ws://HOST:8083/mqtt` / `wss://HOST:8084/mqtt` | Path is always `/mqtt`; check the path your web clients use |
| `listener.ssl.require_certificate = on`, `use_identity_as_username = on` | not available | Move to username/password over TLS |
| `listener.<type>.<name>.mountpoint = mp` | Topic prefix or separate broker | One topic tree per ConnectMQ broker |
| `listener.max_connections`, `listener.<type>.<name>.max_connections` | Plan concurrent connections | Free 5, Basic 100, Scale 1,000 |
| `listener.http.<name>` (status, `/metrics`, HTTP API) | Console dashboard | No Prometheus endpoint; plan new monitoring |
| `allow_anonymous = on` | not available | Every client needs credentials |
| `max_message_size` | Plan max message size | `0` = unlimited on VerneMQ; 256 KB on Free/Basic |
| `max_client_id_size` | Client ID `<ISSUED_ID>_<old-id>` | New IDs are longer: check device limits |
| `persistent_client_expiration` | Plan max session expiry | 1 h / 2 h / 24 h; VerneMQ default is never |
| `max_offline_messages`, `max_online_messages` | Broker-managed | Offline queue lives only as long as the session |
| `max_inflight_messages`, `receive_max_*`, `topic_alias_max_*` | Broker-managed | No client change needed |
| `upgrade_outgoing_qos` | not available | Spec behaviour: min(publish QoS, subscription QoS) |
| `shared_subscription_policy` | `$share/<group>/<filter>` (Basic/Scale) | No local/remote node preference |
| `allow_*_during_netsplit`, `coordinate_registrations` | not applicable | Managed service, no netsplit tuning |
| `nodename`, `distributed_cookie`, `metadata_plugin`, `leveldb.*`, `erlang.*` | not applicable | Cluster internals |
| `systree_enabled` (`$SYS`) | not available | Use the console dashboard and sessions view |

## Plugins

| Plugin | ConnectMQ approach |
| --- | --- |
| `vmq_passwd` (`vmq.passwd`) | New credentials issued in the console; hashes cannot be imported |
| `vmq_acl` (`vmq.acl`) | Access control rules (Basic/Scale); `vernemq_audit.py` converts |
| `vmq_diversity` (Lua; PostgreSQL, MySQL, MongoDB, Redis, HTTP auth) | Not supported. Auth -> ConnectMQ credentials; topic rewrites -> topic design; side effects (DB writes, HTTP calls) -> a subscriber service |
| `vmq_webhooks` (`auth_on_register`, `auth_on_publish`, `auth_on_subscribe`, `on_publish`, `on_deliver`, ...) | Not supported. Auth hooks -> credentials + ACL; event hooks -> a backend that subscribes (shared subscription on Basic/Scale) |
| `vmq_bridge` | Used during the parallel run to bridge VerneMQ -> ConnectMQ; existing bridges re-created or retired |
| `vmq_http_pub` | Publish from the backend with an MQTT client |
| Custom Erlang plugins | Not supported; move logic to a client-side service |

## ACL file (`vmq.acl`)

VerneMQ uses the Mosquitto ACL syntax:

```text
topic read public/#          # before any 'user' line: anonymous clients only
user backend
topic devices/#              # no access keyword = read and write
user dashboard
topic read devices/+/telemetry
pattern write devices/%c/telemetry
pattern read devices/%c/commands
pattern %m/%u/#              # %c client ID, %u username, %m mountpoint
```

Conversion (done by `vernemq_audit.py`):

- `read` -> `subscribe`, `write` -> `publish`, no keyword -> `all`.
- `vmq.acl` has allow lines only; the ConnectMQ no-match policy is set to `deny`, which matches VerneMQ's behaviour
  when `vmq_acl` is the only ACL plugin. If another plugin (diversity, webhooks) also authorises, review the result:
  VerneMQ asks plugins in order and the first decision wins.
- `%c` / `%u` become `${clientid}` / `${username}`, or are expanded per client/user with `--client-map` /
  `--user-map` so topics keep the old IDs.
- `%m` is expanded for the default (empty) mountpoint and every configured mountpoint, with the mountpoint as topic
  prefix.
- Anonymous rules and `$SYS` rules are skipped and listed in the notes.

## Behavioural differences that bite

- **Mountpoints.** Two clients on different mountpoints can publish to `devices/x` without seeing each other. Merged
  into one ConnectMQ broker without a prefix, they suddenly do. Prefix per mountpoint or use separate brokers.
- **Client ID rules.** VerneMQ accepts any ID up to `max_client_id_size`; ConnectMQ accepts only the issued ID or
  `<ISSUED_ID>_<suffix>`.
- **Infinite sessions and big offline queues.** VerneMQ keeps sessions forever by default and queues up to
  `max_offline_messages`. Devices that sleep longer than the plan's session expiry lose queued messages: use
  retained "desired state" topics for configuration instead of queued commands.
- **Session takeover across nodes.** VerneMQ (with `coordinate_registrations`) handles a client reconnecting to
  another node. On ConnectMQ a second connection with the same client ID takes over the session (DISCONNECT `0x8E`):
  watch for this when a device is briefly connected to both brokers during a wave, and never let the relay/bridge
  reuse a device's client ID.
- **Shared subscription node preference.** `prefer_local` / `local_only` have no equivalent; ConnectMQ distributes
  shared-subscription messages across the group's members.
- **Webhook side effects.** `on_publish` hooks often write every message to a database. When devices move, that
  write must happen somewhere else (subscriber service) or data silently stops.
- **Monitoring.** Grafana dashboards built on VerneMQ's `/metrics` stop at cutover; use the ConnectMQ console and
  usage meters, and keep consumer-side metrics.
- **Ordering across publishers.** Per-client order is kept, as the spec requires; order between different
  publishers can interleave differently. Use payload timestamps for cross-device ordering.

## Inventory commands

| Need | Command |
| --- | --- |
| Version | `vernemq version` (or the image tag) |
| Nodes | `vmq-admin cluster show` |
| Sessions, client IDs, users, IPs | `vmq-admin session show --client_id --user --is_online --peer_host --mountpoint` |
| Online count | `vmq-admin session show --is_online \| grep -c true` (or the `socket_open` gauge) |
| Message and byte rates | `vmq-admin metrics show` (`mqtt_publish_received`, `mqtt_publish_sent`, `bytes_received`, `bytes_sent`) or Prometheus `/metrics` |
| Plugins enabled | `vmq-admin plugin show` |
| Config | `vernemq.conf` on each node, or `DOCKER_VERNEMQ_*` env / Helm values |

## Sizing example

Three-node cluster, 600 online sessions at peak, 2 backend consumers, 1 bridge: about 750 concurrent connections
with headroom -> Scale (1,000). Session minutes: 600 always-on devices x 43,200 = 25.9 M per month, far over Scale's
10 M -> ask ConnectMQ for a custom plan, or reduce connected time for devices that only report periodically. Always
check connections and minutes separately.
