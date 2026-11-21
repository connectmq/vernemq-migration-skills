# Cutover, Validation and Rollback Runbook

Source-independent steps for moving clients to ConnectMQ. The source-specific bridge setup is in the other reference
file of this skill.

## Wave order

1. **Wave 0: smoke test.** `verify_connectmq.py` passes on the production ConnectMQ broker with a real credential.
2. **Wave 1: consumers first.** Backend services, dashboards and integrations subscribe on ConnectMQ. With the bridge
   or relay forwarding device traffic, they see the same data as before. Run them in parallel with the old consumers
   (shadow mode) before switching off the old ones; deduplicate if both write to the same database.
3. **Wave 2: canary devices** (about 5 %, or one site). Choose devices you can reach physically or by OTA.
4. **Wave 3+: the rest** in growing waves (25 %, 50 %, 100 %).
5. **Commands last.** Command/response flows (backend -> device) need the reverse bridge direction until every
   device of that group is on ConnectMQ.

Never move a publisher before its consumers can read the data on ConnectMQ.

## Wave sizing

- New connections per second are capped per plan (Free 5, Basic 10, Scale 50). A wave of N devices that reconnect
  together needs at least N / rate seconds; add jitter (random 0-60 s delay) to the device reconnect logic.
- Every device on the old broker that still has old credentials and is pointed at ConnectMQ fails authentication;
  repeated failures from one IP lead to a temporary IP ban (CONNACK `0x8A`), which also blocks good devices behind
  the same NAT. Roll out new credentials **together with** the new host, never the host first.
- Count the bridge/relay, dashboards and test tools in the concurrent-connection budget.

## Go / no-go before each wave

- [ ] Previous wave stable for the agreed soak time (24 h for consumers, 1-2 h for device waves)
- [ ] No unexpected reason codes in client logs (`0x85`, `0x86`, `0x87`, `0x8A`, `0x97`)
- [ ] `compare_streams.py` shows MATCH for the migrated topics
- [ ] ConnectMQ usage (connections, session minutes, traffic) below 80 % of the plan
- [ ] Rollback path tested for this client group
- [ ] On-call knows the wave is running

## Monitoring during a wave

- ConnectMQ console: connected clients, sessions view (who connected, from where, disconnect reasons), usage meters.
- Device side: connect success rate, reconnect count, publish errors.
- Consumers: message rate per topic compared with the previous week, lag, database write rate.
- Source broker: connected clients should fall by the wave size; nothing should still publish there except devices
  not yet migrated and the bridge.

## Rollback triggers

Roll back the current wave (not the whole migration) when any of these hold for more than 10 minutes:

- More than 1 % of the wave cannot connect.
- Message loss or duplication above the agreed tolerance on the migrated topics.
- A consumer cannot keep up or misses data.
- Plan limits (`0x97`) are hit and an upgrade cannot be applied immediately.

## Rollback actions

1. Push the previous configuration (old host and credentials) to the wave's clients. Because the bridge still runs,
   no data is lost: clients on either broker keep exchanging messages.
2. If clients cannot be reconfigured remotely, keep them on ConnectMQ and fix forward (ACL rule, plan upgrade).
3. Record the cause in the migration log and fix it before re-running the wave.

Keep the old broker running and the bridge up until the last wave has soaked. Rollback is only cheap while both run.

## Final cutover

1. Last wave stable for the soak time.
2. Run `copy_retained.py` once more for topics that only the source still holds (should be none if every publisher
   moved).
3. Stop the bridge/relay. Watch for clients that still connect to the source (stragglers): find them in the source
   logs and move them.
4. Keep the source broker read-only (or running without clients) for an agreed period (e.g. 14 days), then
   decommission it and delete stored credentials and certificates.
5. Update DNS, runbooks, monitoring, onboarding docs and device provisioning to create ConnectMQ credentials.
6. Post-migration review: what broke, which reason codes appeared, what to change in the next migration.

## Migration log

Keep a log file (e.g. `migration/log.md`) with one entry per action: date/time, wave, what changed, result, numbers
(clients moved, failures), decision. The executor skill appends to it after every step.
