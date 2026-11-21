# Migration plan: {{SOURCE}} to ConnectMQ

| | |
| --- | --- |
| Owner | {{name}} |
| Date / version | {{YYYY-MM-DD}} / v{{n}} |
| Source | {{SOURCE}} {{version}}, {{deployment: self-hosted / cloud / cluster size}} |
| Target | ConnectMQ {{plan}} plan, broker host {{BROKER_HOST}} |
| Strategy | {{bridge-and-migrate / relay-and-migrate / DNS cutover / big bang (small only)}} |
| Planned window | {{first wave date}} to {{decommission date}} |

## 1. Current state (inventory)

| Item | Value | Source of the number |
| --- | --- | --- |
| Peak concurrent clients | | |
| Total distinct client IDs (30 days) | | |
| Messages in / out per second (avg / peak) | | |
| Largest payload | | |
| Fastest single publisher (msg/s) | | |
| Retained messages (count / total size) | | |
| Subscriptions (total / max per client) | | |
| Persistent sessions and longest offline period | | |
| MQTT versions in use (3.1 / 3.1.1 / 5) | | |
| QoS mix | | |
| Shared subscriptions in use | | |
| Auth method (password / certificate / token / anonymous) | | |
| ACL model and rule count | | |
| Bridges and integrations (consumers, producers, rule engine, plugins) | | |

### Client groups

| Group | Count | Who controls the config | How it is updated (OTA, redeploy, manual) | Downtime tolerance | Wave |
| --- | --- | --- | --- | --- | --- |
| Backend services | | | | | 1 |
| Dashboards / web apps | | | | | 1 |
| Gateways | | | | | 2 |
| Field devices | | | | | 3+ |

## 2. Fit check against ConnectMQ

| Limit | Source workload | Plan limit | OK / Action |
| --- | --- | --- | --- |
| Concurrent connections (incl. bridge/relay + dashboards) | | | |
| Session minutes per month (clients x avg connected minutes) | | | |
| New connections per second during a wave | | | |
| Messages per second per client | | | |
| Max message size | | | |
| Retained messages | | | |
| Subscriptions per client | | | |
| Session expiry vs longest offline period | | | |
| Shared subscriptions / ACL need Basic or Scale | | | |

Blockers and how they are resolved:

| # | Blocker | Resolution | Owner | Status |
| --- | --- | --- | --- | --- |

## 3. Target design

- Credentials: {{one per device / per group / per site}}; issued client IDs: {{list or reference to client-map.csv}}
- Client ID mapping: `<ISSUED_ID>_<old-client-id>` (client-map.csv)
- Topic tree changes: {{none / prefix / redesign}} (topic-map.csv if any)
- ACL: {{rule file}}, no-match policy {{deny/allow}}
- Replacements for features ConnectMQ does not have: {{rule engine -> service X, shadow -> retained state topics, ...}}

## 4. Parallel run

- Bridge/relay: {{source bridge / relay.py on host X}}; forward filters {{...}}; reverse filters {{...}}
- Retained copy: before wave 1 and before final cutover
- Validation: verify_connectmq.py, compare_streams.py for {{duration}}, consumer reconciliation

## 5. Waves

| Wave | Clients | Date | Go criteria | Rollback trigger | Rollback action |
| --- | --- | --- | --- | --- | --- |
| 0 | Test client + one backend consumer | | verify_connectmq passes | any failure | stop |
| 1 | Backend services and dashboards | | no message loss for 24 h | consumer lag / errors | point back to source |
| 2 | 5 % of devices (canary) | | connect success > 99 %, no 0x8x codes | refused connects > 1 % | revert config |
| 3 | 25 % | | | | |
| 4 | 100 % | | | | |

## 6. Cutover and decommission

- Final retained copy, stop bridge/relay, keep the source read-only for {{N}} days
- Decommission date and data retention for the old broker
- DNS / documentation / runbook updates

## 7. Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |

## 8. Communication

Who is told what, when: device owners, on-call, customers.
