# VerneMQ Migration Skills for ConnectMQ

**AI agent skills for planning and executing a migration from a VerneMQ cluster to the [ConnectMQ](https://connectmq.com/?utm_source=github&utm_medium=readme&utm_campaign=migration-skills&utm_content=vernemq) managed MQTT broker.**

Give your AI coding agent (Claude Code, Claude.ai, or any agent that reads `SKILL.md` skills) the knowledge and the
tools of an MQTT migration engineer: it audits `vernemq.conf` and the enabled plugins, converts `vmq.acl`, maps
mountpoints and client IDs, sizes the ConnectMQ plan, writes a phased plan, then sets up the `vmq_bridge`, runs the
client waves and handles rollback.

> Part of the **MQTT Broker Migration Skills** collection:
> [HiveMQ](https://github.com/connectmq/hivemq-migration-skills) ·
> [Mosquitto](https://github.com/connectmq/mosquitto-migration-skills) ·
> [EMQX](https://github.com/connectmq/emqx-migration-skills) ·
> **VerneMQ** ·
> [AWS IoT Core](https://github.com/connectmq/aws-iot-core-migration-skills) ·
> [Azure IoT Hub](https://github.com/connectmq/azure-iot-hub-migration-skills)

## What's inside

| Skill | What it does |
| --- | --- |
| [`vernemq-migration-planner`](skills/vernemq-migration-planner/SKILL.md) | Audits `vernemq.conf`, plugins (`vmq_passwd`, `vmq_acl`, `vmq_diversity`, `vmq_webhooks`, `vmq_bridge`), `vmq.acl` and `vmq.passwd`; samples `vmq-admin` sessions and metrics; checks the workload against ConnectMQ plan limits; converts ACLs; maps mountpoints and client IDs; writes `migration/plan.md` |
| [`vernemq-migration-executor`](skills/vernemq-migration-executor/SKILL.md) | Verifies the ConnectMQ broker, configures `vmq_bridge` (or the external relay), copies retained messages, compares message flow on both brokers, runs client waves with go/no-go gates, rolls back, decommissions the cluster; keeps `migration/log.md` |

### Scripts (Python 3.9+)

| Script | Purpose |
| --- | --- |
| `vernemq_audit.py` | `vernemq.conf` + `vmq.acl` + `vmq.passwd` -> audit report, ConnectMQ ACL rules, user list (standard library only) |
| `client_id_map.py` | Old client IDs -> `<ISSUED_ID>_<old-id>`, flags unsafe characters and collisions |
| `verify_connectmq.py` | Smoke test: TLS connect, QoS 0/1/2, retained, Last Will, persistent session, ACL denies |
| `copy_retained.py` | Copies retained messages from VerneMQ to ConnectMQ (or exports JSON) |
| `compare_streams.py` | Subscribes on both brokers and compares topics, counts and payloads |
| `relay.py` | External two-way bridge with loop protection, for when `vmq_bridge` can't be enabled |

MQTT scripts need `pip install "paho-mqtt>=2.0"`.

## Install

### Claude Code (plugin)

```text
/plugin marketplace add connectmq/vernemq-migration-skills
/plugin install vernemq-migration-skills@connectmq-vernemq-migration
```

### Claude Code (manual)

Copy the folders in `skills/` to `~/.claude/skills/` (all projects) or `.claude/skills/` (one project):

```bash
git clone https://github.com/connectmq/vernemq-migration-skills
cp -r vernemq-migration-skills/skills/* ~/.claude/skills/
```

### Claude.ai

Zip a skill folder (e.g. `skills/vernemq-migration-planner`) and upload it under Settings > Capabilities > Skills.

### Other agents

Each skill is a self-contained folder with a `SKILL.md` (YAML front matter + instructions), `references/` and
`scripts/`. Agents that support the Agent Skills format can load the folders directly; for others, add the
`SKILL.md` content to the agent's instructions.

## Example prompts

- "We run a three-node VerneMQ cluster on Kubernetes. Plan a move to ConnectMQ."
- "Audit our vernemq.conf and tell me what blocks a migration to ConnectMQ."
- "Convert vmq.acl to ConnectMQ access control rules; we use the factory mountpoint."
- "What do our vmq_webhooks do, and what replaces them on ConnectMQ?"
- "Configure vmq_bridge to our ConnectMQ broker for telemetry and commands."
- "Run wave 2 and check that nothing is lost."

## How the migration works

1. **Plan**: audit config and plugins, measure sessions and metrics, check plan limits, map credentials, client
   IDs, mountpoints and ACLs, choose waves.
2. **Bridge**: one VerneMQ node bridges forward topics to ConnectMQ and commands back.
3. **Consumers first**: backends and dashboards read from ConnectMQ while devices are still on VerneMQ; webhook side
   effects move to a subscriber service.
4. **Devices in waves**: canary, then growing waves, each with go/no-go and rollback.
5. **Cutover**: disable the bridge, keep the cluster for the rollback window, decommission.

Key ConnectMQ facts the skills plan around: credentials are generated in the console and come with an issued client
ID (devices connect as `<ISSUED_ID>_<suffix>`), authentication is username/password over TLS (no client
certificates, no auth plugins or webhooks), per-topic ACL and shared subscriptions are on the Basic and Scale plans,
there is one topic tree per broker (no mountpoints), and each plan caps connections, session minutes, connection
rate, message size, retained messages and session expiry. See
[plans and limits](https://github.com/connectmq/connectmq/wiki/Plans-and-Limits).

## Repository layout

```
.claude-plugin/              plugin + marketplace manifests (Claude Code)
skills/
  vernemq-migration-planner/
    SKILL.md
    references/              connectmq-target.md, vernemq-mapping.md, cutover-and-rollback.md
    assets/                  migration-plan-template.md
    scripts/                 vernemq_audit.py, client_id_map.py, connectmq_acl.py
  vernemq-migration-executor/
    SKILL.md
    references/              connectmq-target.md, vernemq-bridge.md, cutover-and-rollback.md
    scripts/                 verify_connectmq.py, relay.py, copy_retained.py, compare_streams.py, ...
```

## Learn more

- Blog: [VerneMQ Migration Skills: Move a VerneMQ Cluster to ConnectMQ with an AI Agent](https://connectmq.com/blog/vernemq-migration-skills/?utm_source=github&utm_medium=readme&utm_campaign=migration-skills&utm_content=vernemq)
- [ConnectMQ docs (wiki)](https://github.com/connectmq/connectmq/wiki) · [MQTT code samples](https://github.com/connectmq/mqtt-code-samples)
- Start free (no credit card): [app.connectmq.com/signup](https://app.connectmq.com/signup?utm_source=github&utm_medium=readme&utm_campaign=migration-skills&utm_content=vernemq)

## Contributing

Issues and pull requests are welcome: migration patterns, VerneMQ settings the audit does not know yet, fixes to the
scripts. Please do not include real credentials or customer data in examples.

## License

MIT. VerneMQ is a trademark of its respective owners; this project is not affiliated with or endorsed by the VerneMQ
project.
