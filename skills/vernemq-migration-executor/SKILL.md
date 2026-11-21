---
name: vernemq-migration-executor
description: Executes a planned migration from a VerneMQ cluster to the ConnectMQ managed MQTT broker step by step. Verifies the ConnectMQ broker, enables and configures the vmq_bridge plugin (or the external relay) to ConnectMQ, copies retained messages, validates message flow on both brokers, moves clients in waves with go/no-go checks, handles rollback and decommissions the VerneMQ cluster, keeping a migration log. Use when a VerneMQ-to-ConnectMQ plan exists and the team is ready to set up the bridge, run a wave, cut over, roll back or troubleshoot connection errors during the move.
license: MIT
metadata:
  source-broker: vernemq
  target-broker: connectmq
  companion-skill: vernemq-migration-planner
---

# VerneMQ to ConnectMQ: Migration Executor

You carry out a VerneMQ to ConnectMQ migration safely, one verified step at a time. You change configuration, run
the scripts, read the results, and stop for a human decision at every go/no-go point.

Before starting, read:

- `migration/plan.md` (from the planner skill). If it does not exist, offer to run the
  **vernemq-migration-planner** skill first; do not improvise waves without a plan.
- `references/connectmq-target.md` for limits and reason codes.
- `references/vernemq-bridge.md` for the bridge configuration.
- `references/cutover-and-rollback.md` for wave rules, go/no-go and rollback.

## Rules

- **Ask before every production change** (plugin enable, config reload, ACL change, client wave, node shutdown), and
  say what will change and how to undo it.
- Never put passwords in files that get committed. Use environment variables (`CONNECTMQ_USERNAME`,
  `CONNECTMQ_PASSWORD`, `CONNECTMQ_CLIENT_ID`, `SOURCE_USERNAME`, `SOURCE_PASSWORD`), Kubernetes secrets or a
  secrets store.
- After each step append an entry to `migration/log.md`: time, step, command, result, numbers, decision.
- If a check fails, stop, diagnose with the reason-code table and propose a fix or a rollback. Never continue to the
  next wave with a failing check.

## Scripts

All in `scripts/` (Python 3.9+, `pip install "paho-mqtt>=2.0"`):

| Script | Purpose |
| --- | --- |
| `verify_connectmq.py` | Connect, QoS 0/1/2, retained, will, session, ACL deny checks |
| `copy_retained.py` | Copy retained messages from VerneMQ to ConnectMQ (or export/import JSON) |
| `compare_streams.py` | Subscribe on both brokers and compare topics, counts and payloads |
| `relay.py` | External bridge when `vmq_bridge` cannot be enabled |
| `client_id_map.py` | Old -> new client ID mapping (if not done in planning) |

The scripts connect to VerneMQ as ordinary MQTT clients: give them a VerneMQ user that the ACL allows to read the
forward topics (and `#` for `copy_retained.py`).

## Phase 1: Prepare ConnectMQ

1. Confirm the plan in `migration/plan.md` matches the console (limits, ACL and shared subscriptions available).
2. Create the credentials listed in the plan, plus one for the bridge (or a `_bridge` suffix) and one for tests.
3. Enter ACL rules from `migration/acl-rules.md` in order, set the no-match policy, and **add the bridge rules
   first** (publish on forward topics including any mountpoint prefix, subscribe on reverse topics). Enable ACL only
   when the rules are in.
4. Smoke test:

   ```bash
   export CONNECTMQ_USERNAME=... CONNECTMQ_PASSWORD=... CONNECTMQ_CLIENT_ID=...
   python scripts/verify_connectmq.py --url mqtts://BROKER_HOST:8883 --prefix <a branch the test credential may use>
   ```

   Add `--expect-deny-publish` / `--expect-deny-subscribe` for topics the ACL must block. If the persistent-session
   check fails, check the plan's session expiry before relying on offline queueing.

## Phase 2: Bridge

1. Write the bridge settings from `references/vernemq-bridge.md` with the plan's forward/reverse topics and
   mountpoint prefixes.
2. Test the settings on a staging VerneMQ (or a throwaway container) first.
3. Apply on **one** production node. With Docker/Helm, add the settings as `DOCKER_VERNEMQ_*` variables on a single
   pod (or a dedicated bridge node joined to the cluster), not on the whole StatefulSet. Enable the plugin at
   runtime with `vmq-admin plugin enable --name vmq_bridge` once the settings are present, or restart that node in
   a quiet period.
4. Check: `vmq-admin bridge show` lists the bridge; the ConnectMQ console shows the bridge client ID online.
5. Copy retained state: `python scripts/copy_retained.py --source mqtt://VERNEMQ_HOST:1883 --filter '#' --export migration/retained-<date>.json --target mqtts://BROKER_HOST:8883 --limit <plan retained limit>`
   (start with `--dry-run`, filter out test and junk topics; add `--add-prefix` for mountpoint prefixes).
6. Validate for at least one business cycle:
   `python scripts/compare_streams.py --source ... --target ... --filter '<forward filters>' --duration 600`.
   Expect MATCH. Investigate any "only on source" topic (bridge ACL deny, filter typo, payload above plan size).

## Phase 3: Waves

For each wave in the plan:

1. Run the go/no-go checklist from `references/cutover-and-rollback.md` and get an explicit "go" from the user.
2. Change the clients of the wave: host `BROKER_HOST`, port 8883 with TLS, new username/password, client ID from
   `migration/client-map.csv`, topics with the mountpoint prefix if the plan says so. Show the exact config diff for
   each client type.
3. Watch for 15 minutes: ConnectMQ connected count rises by the wave size; `vmq-admin session show --is_online`
   falls; no `0x85/0x86/0x87/0x8A/0x97` in client logs; `compare_streams.py` still MATCH.
4. Make sure webhook/diversity side effects for the moved devices now happen in their replacement (for example
   database writes from a subscriber service), not only on VerneMQ.
5. Soak for the agreed time, log the numbers, then propose the next wave. On a rollback trigger, follow the rollback
   actions and log the cause.

Typical client changes:

```text
host: vernemq-lb.internal     -> BROKER_HOST
port: 1883                    -> 8883 (TLS, verify hostname, system CA store)
username/password: old        -> ConnectMQ credential
client_id: sensor-0042        -> <ISSUED_ID>_sensor-0042
topic: devices/0042/telemetry -> factory/devices/0042/telemetry   (only if the device used mountpoint "factory")
keepalive: >600               -> <= 600
reconnect: fixed delay        -> exponential backoff with jitter, max 60 s
```

## Phase 4: Cutover and decommission

Follow "Final cutover" in `references/cutover-and-rollback.md`: final retained copy, remove the bridge settings and
disable the plugin (`vmq-admin plugin disable --name vmq_bridge`), watch `vmq-admin session show --is_online` for
stragglers, keep the cluster available for the agreed rollback window, then scale it down and delete stored
credentials, certificates and webhook endpoints. Replace `/metrics` dashboards and alerts.

## Troubleshooting quick reference

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Bridge never connects, TLS error in `console.log` | CA not configured for the bridge | Set the bridge `cafile`/`capath` to the system CA bundle |
| Bridge refused `0x86` / bad credentials | Wrong bridge username/password | Copy from the console |
| Bridge connects and drops at once | Client ID not `<ISSUED_ID>_<suffix>` (0x85) or bridge configured on several nodes (0x8E) | Fix `client_id`; bridge on one node only |
| Nothing arrives on ConnectMQ | ACL denies bridge publish, direction wrong (`in` vs `out`), mountpoint not matched | Add bridge ACL rule; `out` = VerneMQ -> ConnectMQ; check prefix |
| Duplicates / loops | `both` direction or overlapping in/out patterns | Split topics by direction |
| Devices `0x8A` banned | Wrong credentials retried in a loop from one NAT | Stop the wave, fix config, wait for the ban to expire |
| `0x97` on connect | Plan connections or minutes exhausted | Upgrade plan or slow the wave |
| `0x97` on publish | Payload above plan max size or per-client rate | Reduce payload/rate, or custom plan |
| Data stops in the database after a wave | Webhook/diversity side effect only on VerneMQ | Enable the replacement subscriber before the wave |
