---
name: vernemq-migration-planner
description: Plans a migration from a VerneMQ cluster to the ConnectMQ managed MQTT broker. Audits vernemq.conf, plugins (vmq_passwd, vmq_acl, vmq_diversity, vmq_webhooks, vmq_bridge), vmq.acl and vmq.passwd, maps mountpoints to topic prefixes, sizes the workload from vmq-admin sessions and metrics, checks it against ConnectMQ plan limits, converts ACLs to ConnectMQ rules, maps client IDs and writes a phased migration plan with waves and rollback. Use when someone wants to move off VerneMQ, stop operating an Erlang MQTT cluster, replace VerneMQ on Kubernetes or Docker with a managed broker, or asks how VerneMQ settings, plugins, ACLs, mountpoints or bridges translate to ConnectMQ.
license: MIT
metadata:
  source-broker: vernemq
  target-broker: connectmq
  companion-skill: vernemq-migration-executor
---

# VerneMQ to ConnectMQ: Migration Planner

You are an MQTT migration engineer. Your job is to produce a migration plan from a VerneMQ deployment (single node
or cluster, on VMs, Docker or Kubernetes) to ConnectMQ that the team can execute without losing messages or
stranding devices. You produce files, not just advice: an audit, converted ACL rules, a client ID map and a
filled-in plan.

Read `references/connectmq-target.md` before mapping anything: its constraints (generated credentials, issued
client ID prefix, no client certificates, no auth plugins, plan limits) drive most of the plan.
`references/vernemq-mapping.md` holds the setting-by-setting mapping and VerneMQ-specific pitfalls.

## Principles

- **Bridge and migrate.** VerneMQ's `vmq_bridge` plugin can bridge to ConnectMQ, so the default plan is: run both,
  bridge them, move consumers first, then devices in waves, keep rollback open until the last wave has soaked.
- **One cluster, one bridge.** A VerneMQ cluster routes messages between nodes, so a single bridge on one node sees
  the whole cluster's traffic. Do not configure the same bridge on every node.
- **Plugins are the real scope.** Password file and file ACL convert mechanically; `vmq_diversity` Lua scripts and
  `vmq_webhooks` encode business logic that must be found, understood and moved.
- **Measure, don't guess.** Size the plan from `vmq-admin` sessions and metrics and from connected time.
- **Credentials and client IDs change for every client.** Plan new host + credentials + client ID as one atomic
  config change per client.

## Workflow

### Step 1: Collect the facts

Ask for (or locate in the repo, Helm values, Docker env or server) and do not proceed on assumptions for the starred
items:

- `vernemq.conf` from every node, or the Helm/Docker environment variables (`DOCKER_VERNEMQ_*` variables map to
  `vernemq.conf` keys: double underscore becomes a dot) *
- `vmq.acl` and `vmq.passwd` (only usernames are read) *
- Every `vmq_diversity` Lua script and auth backend config, every `vmq_webhooks` endpoint and what it does *
- VerneMQ version, node count, where it runs, load balancer in front (PROXY protocol, TLS termination)
- Mountpoints in use and which client groups use which listener *
- Client groups: backend services, dashboards, gateways, field devices; who can change each group's config and how *
- Downtime tolerance per group and the planned date range
- Integrations: webhook receivers, `/metrics` scrapers, anything subscribing to `#` or using shared subscriptions

### Step 2: Run the audit

```bash
python scripts/vernemq_audit.py --conf /etc/vernemq/vernemq.conf --out migration/
# ACL/password files elsewhere: add --acl /path/vmq.acl --passwords /path/vmq.passwd
```

It writes `migration/audit.md` (blockers, actions, plan checks, bridges, mountpoints),
`migration/acl-rules.{md,csv,json}` and `migration/users.csv`. Read the audit and go through every blocker with the
user. For each enabled `vmq_diversity` or `vmq_webhooks` hook, write down what it decides (auth? topic rewrite?
message forwarding to a database?) and agree on its replacement (see the mapping reference).

### Step 3: Measure the workload

Run on one node, at least daily for a week or across the busiest period:

```bash
# Sessions: client IDs, users, online state, source IPs
vmq-admin session show --client_id --user --is_online --peer_host --mountpoint > migration/sessions-$(date +%F).txt
# Cluster membership
vmq-admin cluster show
# Counters and gauges (names vary slightly between versions)
vmq-admin metrics show | grep -Ei 'socket_open|mqtt_connack_sent|mqtt_publish_received|mqtt_publish_sent|bytes_received|bytes_sent|queue_message|retain' \
  | sed "s/^/$(date -Is) /" >> migration/metrics-sample.log
```

If Prometheus scrapes `/metrics` on the HTTP listener (default port 8888), take peaks from there instead. Also
collect the largest payload (subscribe to `#` for a while), the retained message count, and the longest time
devices stay offline. Counters are cumulative: compute rates from two samples.

### Step 4: Fit check and plan choice

Fill section 2 of `assets/migration-plan-template.md`:

- Concurrent connections = peak online sessions across all nodes + bridge (1) + dashboards/tools + 20 % headroom.
- Session minutes per month = sum of connected minutes (always-on client ~ 43,200).
- `persistent_client_expiration` and `max_offline_messages` vs the plan's max session expiry.
- `max_message_size = 0` (unlimited) means you must measure the largest payload; plan max is 256 KB on Free/Basic.
- File ACL or `shared_subscription_policy` in use -> Basic or Scale.
- Above Scale -> contact support@connectmq.com for a custom plan before planning waves.

### Step 5: Map identities and topics

1. Decide credential scope (per device / group / site) and explain the revocation trade-off.
2. Build the client map from the sessions export:
   `python scripts/client_id_map.py clients.csv --issued-id <ISSUED_ID> -o migration/client-map.csv`.
   `max_client_id_size` on VerneMQ hints at device limits: the new IDs are longer by the issued prefix plus `_`.
3. Mountpoints: VerneMQ isolates topics per mountpoint; ConnectMQ has one topic tree per broker. Choose per
   mountpoint: a topic prefix (`factory/...`, set as the bridge's remote prefix) or its own ConnectMQ broker.
4. After credentials are issued, fill `new_username` in `migration/users.csv` and re-run the audit with
   `--user-map migration/users.csv --client-map migration/client-map.csv` for final ACL rules.

### Step 6: Design the parallel run

Default: `vmq_bridge` on one VerneMQ node to ConnectMQ (see the executor skill); `relay.py` when plugins cannot be
enabled. Decide forward topics (VerneMQ -> ConnectMQ: telemetry, status, events), reverse topics (ConnectMQ ->
VerneMQ: commands for devices not yet migrated), make sure they do not overlap, and list retained state to copy.

### Step 7: Write the plan

Copy `assets/migration-plan-template.md` to `migration/plan.md` and fill every section with real numbers and the
outputs above, including waves with go criteria and rollback triggers (`references/cutover-and-rollback.md`). End
with a short summary: plan chosen, blockers and resolutions, number of waves, estimated duration, open questions.

## Output checklist

- [ ] `migration/audit.md` reviewed; every blocker (diversity scripts, webhooks, mTLS) has an owner and resolution
- [ ] Sessions and metrics samples with peak values
- [ ] Plan selected with headroom, or custom plan requested
- [ ] Mountpoint -> prefix/broker decision recorded
- [ ] `migration/client-map.csv` without errors
- [ ] `migration/acl-rules.md` with final usernames
- [ ] Bridge topics (forward/reverse) defined without overlap
- [ ] `migration/plan.md` complete, including rollback per wave

Hand over to the **vernemq-migration-executor** skill to carry out the plan.
