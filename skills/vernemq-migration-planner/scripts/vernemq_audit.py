#!/usr/bin/env python3
"""Audits a VerneMQ installation for a migration to ConnectMQ.

Reads vernemq.conf (key = value), the vmq_acl file, the vmq_passwd file (usernames only, never hashes) and writes:

  <out>/audit.md          findings: settings and plugins mapped to ConnectMQ, with blockers and actions
  <out>/acl-rules.md/.csv/.json   ConnectMQ access control rules converted from vmq.acl
  <out>/users.csv         old usernames, with an empty column for the new ConnectMQ username (fill in, then pass
                          back with --user-map to get rules for the new usernames)

Examples:
  python vernemq_audit.py --conf /etc/vernemq/vernemq.conf --out migration/
  python vernemq_audit.py --conf vernemq.conf --client-map client-map.csv --user-map users.csv --out migration/
  python vernemq_audit.py --acl vmq.acl --passwords vmq.passwd --out migration/        # without vernemq.conf

--client-map: CSV old_client_id,new_client_id (from client_id_map.py). Pattern rules using %c are expanded into
one rule per client so topics keep the old client ID.
--user-map:   CSV old_username,new_username. Rules for a user get the new ConnectMQ username as subject; pattern
rules with %u are expanded per user.
--mountpoint-prefix: how mountpoints become topic prefixes on ConnectMQ, e.g. "{mp}/" (default) or "" to drop them.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectmq_acl import Rule, check, dedupe, expand_pattern, load_map, render  # noqa: E402

# (regex on the key, severity, ConnectMQ mapping / action). First match wins.
MAPPING = [
    (r"^listener\.(http|https)\.", "info", "VerneMQ HTTP listener (status page, /metrics, HTTP API) has no ConnectMQ equivalent; use the console dashboard."),
    (r"^listener\.([^.]+\.)*mountpoint$", "action", "Mountpoints isolate topic trees per listener. ConnectMQ has one tree per broker: map each mountpoint to a topic prefix (or a separate broker)."),
    (r"^listener\.([^.]+\.)*(require_certificate)$", "blocker", "Client certificates (mTLS) are not supported. Devices move to username/password over TLS."),
    (r"^listener\.([^.]+\.)*(use_identity_as_username)$", "blocker", "Certificate identity as username is not supported; issue credentials per device."),
    (r"^listener\.([^.]+\.)*(psk_file|use_psk|psk_identity_hint)$", "blocker", "TLS-PSK is not supported; use TLS with the public server certificate and username/password."),
    (r"^listener\.([^.]+\.)*(cafile|certfile|keyfile|depth|ciphers|tls_version|crlfile)$", "info", "Server TLS is managed by ConnectMQ; clients can use the OS CA store."),
    (r"^listener\.([^.]+\.)*(max_connections)$", "check", "Compare with the plan's concurrent connections (Free 5, Basic 100, Scale 1,000)."),
    (r"^listener\.([^.]+\.)*(proxy_protocol|proxy_protocol_use_cn_as_username)$", "info", "Load balancer PROXY protocol is not applicable on ConnectMQ."),
    (r"^listener\.tcp\.[^.]+$", "info", "ConnectMQ listens on 8883 (TLS), 8084 (wss, /mqtt), 1883 and 8083 (tests only). Clients change host and port."),
    (r"^listener\.ssl\.[^.]+$", "info", "TLS listener -> mqtts://HOST:8883. ConnectMQ's certificate is from a public CA."),
    (r"^listener\.ws\.[^.]+$", "info", "WebSocket listener -> ws://HOST:8083/mqtt (tests) or wss://HOST:8084/mqtt."),
    (r"^listener\.wss\.[^.]+$", "info", "Secure WebSocket listener -> wss://HOST:8084/mqtt (path is always /mqtt)."),
    (r"^listener\.", "info", "Listener tuning (acceptors, buffers) is managed by ConnectMQ."),
    (r"^allow_anonymous$", "action", "ConnectMQ has no anonymous access: every client needs credentials issued in the console."),
    (r"^max_message_size$", "check", "Plan max message size (256 KB on Free/Basic). 0 means unlimited on VerneMQ: find the largest payload."),
    (r"^max_inflight_messages$", "info", "ConnectMQ sets receive-maximum per plan; no client change needed."),
    (r"^max_online_messages$", "info", "Per-session online queue is broker-managed on ConnectMQ."),
    (r"^max_offline_messages$", "check", "Offline queueing is limited by the plan's session expiry (1 h / 2 h / 24 h)."),
    (r"^persistent_client_expiration$", "check", "ConnectMQ caps session expiry per plan (Free 1 h, Basic 2 h, Scale 24 h). VerneMQ default is never: devices offline longer lose queued messages."),
    (r"^max_client_id_size$", "action", "ConnectMQ client IDs are <ISSUED_ID>_<old-id>: the new IDs are longer; check device firmware limits."),
    (r"^allow_(register|publish|subscribe|unsubscribe)_during_netsplit$", "info", "Netsplit tuning is not applicable: ConnectMQ is operated as a managed service."),
    (r"^(nodename|distributed_cookie|erlang\.|metadata_plugin|vmq_swc\.|leveldb\.|message_store|cluster)", "info", "Cluster/Erlang/storage settings are not applicable on a managed broker."),
    (r"^shared_subscription_policy$", "check", "Shared subscriptions ($share/<group>/<filter>) need Basic or Scale; VerneMQ's local/remote preference has no equivalent."),
    (r"^upgrade_outgoing_qos$", "info", "No equivalent; subscribers get min(publish QoS, subscription QoS) as in the spec."),
    (r"^retry_interval$", "info", "QoS retries are broker-managed."),
    (r"^(max_keepalive|max_keep_alive)", "info", "ConnectMQ caps keep-alive at 600 s."),
    (r"^(max_last_will_delay|max_message_rate|receive_max_|topic_alias_max_|suppress_lwt_on_session_takeover|coordinate_registrations)", "info", "Broker-managed on ConnectMQ; check client behaviour in the smoke test."),
    (r"^systree_", "info", "$SYS tree is not provided; use the ConnectMQ dashboard and sessions view."),
    (r"^(log\.|graphite_|outgoing_clustering|accept_eventually)", "info", "Not applicable."),
    (r"^vmq_acl\.acl_file$", "action", "Converted to ConnectMQ access control rules (acl-rules.md). Per-topic ACL needs Basic or Scale."),
    (r"^vmq_acl\.", "info", "ACL reload settings are not applicable; rules are edited in the ConnectMQ console."),
    (r"^vmq_passwd\.password_file$", "action", "Usernames/passwords cannot be imported. Issue new ConnectMQ credentials (see users.csv)."),
    (r"^vmq_passwd\.", "info", "Not applicable."),
    (r"^vmq_diversity\.", "blocker", "vmq_diversity (Lua scripts, PostgreSQL/MySQL/MongoDB/Redis auth) does not run on ConnectMQ. Move users to ConnectMQ credentials and logic into a client-side service."),
    (r"^vmq_webhooks\.", "blocker", "Webhooks (auth_on_register, on_publish, ...) are not supported. Replace auth with ConnectMQ credentials and event hooks with a subscriber service."),
    (r"^vmq_bridge\.", "action", "Existing bridge: re-create or retire it. During the migration a bridge from VerneMQ to ConnectMQ carries the parallel run."),
    (r"^vmq_http_pub\.", "blocker", "HTTP publish API is not available; publish with an MQTT client from the backend."),
]

PLUGINS = {
    "vmq_passwd": ("action", "Password file auth -> ConnectMQ credentials (new usernames and passwords)."),
    "vmq_acl": ("action", "File ACL -> ConnectMQ access control rules (Basic/Scale)."),
    "vmq_diversity": ("blocker", "Lua / database auth and hooks are not supported; redesign."),
    "vmq_webhooks": ("blocker", "Webhook auth and event hooks are not supported; redesign."),
    "vmq_bridge": ("action", "Bridge plugin: the tool for the parallel run (bridge to ConnectMQ)."),
    "vmq_http_pub": ("blocker", "HTTP publish API not available."),
}


def read_conf(path: str) -> list[tuple[str, str, int]]:
    """Returns (key, value, line number) for every 'key = value' line."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out.append((key.strip(), value.strip(), n))
    return out


def classify(key: str, value: str) -> tuple[str, str]:
    m = re.match(r"^plugins\.([a-z0-9_]+)$", key)
    if m:
        name = m.group(1)
        if value.lower() not in ("on", "true"):
            return "info", f"Plugin {name} is off on the source; nothing to migrate."
        return PLUGINS.get(name, ("blocker", f"Custom plugin {name}: plugins do not run on ConnectMQ; review what it does."))
    for pattern, sev, text in MAPPING:
        if re.search(pattern, key):
            if sev == "blocker" and re.search(r"(require_certificate|use_identity_as_username|use_psk)$", key) \
                    and value.lower() in ("off", "false"):
                return "info", "Disabled on the source; nothing to migrate."
            if key == "allow_anonymous" and value.lower() in ("off", "false"):
                return "info", "Disabled on the source; nothing to migrate."
            return sev, text
    return "unknown", "Review manually."


def parse_acl_file(path: str, notes: list[str]) -> list[tuple[str | None, str, str, str]]:
    """Returns (user or None, kind 'topic'|'pattern', access, topic). VerneMQ uses the Mosquitto syntax:
    'topic [read|write] <topic>', 'user <name>', 'pattern [read|write] <topic>' with %c, %u, %m. Topic lines before the
    first 'user' line apply to anonymous clients; pattern lines apply to everyone."""
    entries, user = [], None
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            kw, rest = parts[0], (parts[1] if len(parts) > 1 else "")
            if kw == "user":
                user = rest.strip()
            elif kw in ("topic", "pattern"):
                tokens = rest.split(None, 1)
                if tokens and tokens[0] in ("read", "write", "readwrite", "deny") and len(tokens) > 1:
                    access, topic = tokens[0], tokens[1].strip()
                    if access in ("readwrite", "deny"):
                        notes.append(f"vmq.acl line {n}: '{access}' is Mosquitto syntax that vmq_acl does not document; "
                                     "converted as written, verify the intent.")
                else:
                    access, topic = "readwrite", rest.strip()
                entries.append((user if kw == "topic" else "*", kw, access, topic))
            else:
                notes.append(f"vmq.acl line {n}: unknown keyword '{kw}' ignored.")
    return entries


ACCESS = {"read": ["subscribe"], "write": ["publish"], "readwrite": ["all"], "deny": ["all"]}


def acl_to_rules(entries, user_map, client_map, mountpoints, mp_prefix, notes):
    deny, allow = [], []
    anon = [e for e in entries if e[0] is None]
    if anon:
        notes.append(f"{len(anon)} rule(s) for anonymous clients were skipped: ConnectMQ has no anonymous access. "
                     "Give those clients credentials and add rules for them.")
    for user, kind, access, topic in entries:
        if user is None:
            continue
        if topic.startswith("$SYS"):
            notes.append(f"Skipped $SYS rule '{topic}' ({user}): ConnectMQ does not publish $SYS topics.")
            continue
        topics = [topic]
        if "%m" in topic:
            # The default (empty) mountpoint: %m disappears; named mountpoints become a topic prefix
            topics = [topic.replace("%m/", "").replace("/%m", "").replace("%m", "")]
            topics += [topic.replace("%m", (mp_prefix.format(mp=mp).rstrip("/") if mp_prefix else mp))
                       for mp in mountpoints]
            notes.append(f"'{topic}': %m expanded for the default mountpoint"
                         + (f" and {', '.join(mountpoints)}" if mountpoints else "")
                         + "; on ConnectMQ a named mountpoint becomes a topic prefix.")
        for raw_t in topics:
            t = raw_t.replace("%c", "${clientid}").replace("%u", "${username}")
            for action in ACCESS[access]:
                perm = "deny" if access == "deny" else "allow"
                if kind == "pattern":
                    rule = Rule(perm, action, t, "all", "", f"pattern {access} {topic}")
                    rules = [rule]
                    if "${clientid}" in t:
                        if client_map:
                            rules = expand_pattern(rule, client_map, "${clientid}")
                        else:
                            rule.warnings.append("${clientid} is now the full ConnectMQ ID (<ISSUED>_<old>); pass "
                                                 "--client-map to keep old IDs in topics")
                    if "${username}" in t:
                        if user_map:
                            expanded = []
                            for r in rules:
                                for x in expand_pattern(r, user_map, "${username}"):
                                    if x is not r:
                                        x.subject_type, x.subject_value = "username", x.subject_value
                                    expanded.append(x)
                            rules = expanded
                        else:
                            for r in rules:
                                r.warnings.append("${username} is now the generated ConnectMQ username; pass --user-map")
                else:
                    new_user = user_map.get(user)
                    warnings = [] if new_user else [f"old username '{user}': replace with its ConnectMQ username"]
                    rules = [Rule(perm, action, t, "username", new_user or user, f"user {user}: topic {access} {topic}",
                                  warnings)]
                (deny if perm == "deny" else allow).extend(rules)
    return dedupe(deny + allow)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", help="vernemq.conf")
    ap.add_argument("--acl", help="vmq_acl file (default: vmq_acl.acl_file from vernemq.conf)")
    ap.add_argument("--passwords", help="vmq_passwd file (default: vmq_passwd.password_file from vernemq.conf)")
    ap.add_argument("--client-map", help="CSV old_client_id,new_client_id")
    ap.add_argument("--user-map", help="CSV old_username,new_username")
    ap.add_argument("--mountpoint-prefix", default="{mp}/", help='Topic prefix per mountpoint (default "{mp}/")')
    ap.add_argument("--out", default="migration", help="Output directory (default ./migration)")
    args = ap.parse_args()
    if not (args.conf or args.acl):
        ap.error("give --conf or --acl")
    os.makedirs(args.out, exist_ok=True)

    client_map = load_map(args.client_map, "old_client_id", "new_client_id")
    user_map = {k: v for k, v in load_map(args.user_map, "old_username", "new_username").items() if v}

    settings = read_conf(args.conf) if args.conf else []
    conf_dir = os.path.dirname(os.path.abspath(args.conf)) if args.conf else "."
    values = {k: v for k, v, _ in settings}

    def resolve(p):
        if not p:
            return None
        if os.path.isabs(p):
            return p
        # VerneMQ resolves ./etc/... relative to its install dir; try next to vernemq.conf as well
        for base in (conf_dir, os.path.dirname(conf_dir)):
            cand = os.path.normpath(os.path.join(base, p))
            if os.path.exists(cand):
                return cand
        return os.path.normpath(os.path.join(conf_dir, os.path.basename(p)))

    acl_path = args.acl or resolve(values.get("vmq_acl.acl_file"))
    pw_path = args.passwords or resolve(values.get("vmq_passwd.password_file"))

    findings = {"blocker": [], "action": [], "check": [], "info": [], "unknown": []}
    bridges: dict[str, list[str]] = {}
    mountpoints = sorted({v for k, v, _ in settings if k.endswith(".mountpoint") and v})
    for key, value, n in settings:
        b = re.match(r"^vmq_bridge\.(tcp|ssl)\.([^.]+)(\.(.+))?$", key)
        if b:
            name = f"{b.group(2)} ({b.group(1)})"
            sub = b.group(4) or "address"
            shown = "***" if sub in ("password", "psk") else value
            bridges.setdefault(name, []).append(f"{sub} = {shown}")
            continue
        sev, text = classify(key, value)
        shown = "***" if re.search(r"(password|cookie|secret|psk)", key) else value
        findings[sev].append(f"`{key} = {shown}` (line {n}): {text}")
    if args.conf and "allow_anonymous" not in values:
        findings["info"].append("`allow_anonymous` not set (VerneMQ default off).")

    notes: list[str] = []
    rules: list[Rule] = []
    if acl_path and os.path.exists(acl_path):
        rules = acl_to_rules(parse_acl_file(acl_path, notes), user_map, client_map, mountpoints,
                             args.mountpoint_prefix, notes)
    elif acl_path:
        notes.append(f"ACL file {acl_path} not found; pass --acl.")
    users = []
    if pw_path and os.path.exists(pw_path):
        with open(pw_path, encoding="utf-8", errors="replace") as f:
            users = sorted({line.split(":", 1)[0].strip() for line in f if ":" in line and not line.startswith("#")})
    elif pw_path:
        notes.append(f"Password file {pw_path} not found; pass --passwords.")

    problems = check(rules)
    if rules:
        for fmt, ext in (("markdown", "md"), ("csv", "csv"), ("json", "json")):
            with open(os.path.join(args.out, f"acl-rules.{ext}"), "w", encoding="utf-8", newline="") as f:
                f.write(render(rules, "deny", fmt, notes + problems))
    if users:
        with open(os.path.join(args.out, "users.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["old_username", "new_username", "credential_scope"])
            for u in users:
                w.writerow([u, user_map.get(u, ""), ""])

    lines = ["# VerneMQ -> ConnectMQ audit", ""]
    if args.conf:
        lines.append(f"Configuration: `{args.conf}` ({len(settings)} settings)")
    lines += [f"ACL file: `{acl_path or '-'}`", f"Password file: `{pw_path or '-'}` ({len(users)} users)",
              f"Mountpoints: {', '.join(mountpoints) if mountpoints else 'none (default empty mountpoint)'}", ""]
    titles = {"blocker": "Blockers (must be redesigned)", "action": "Actions (change during migration)",
              "check": "Check against the ConnectMQ plan", "info": "Information", "unknown": "Not recognised (review)"}
    for sev in ("blocker", "action", "check", "info", "unknown"):
        if findings[sev]:
            lines += [f"## {titles[sev]}", ""] + [f"- {x}" for x in findings[sev]] + [""]
    if bridges:
        lines += ["## Existing bridges (vmq_bridge)", "",
                  "Each bridge must be re-created or retired. Keep it running during the parallel run if its remote "
                  "broker stays; otherwise move that traffic to ConnectMQ.", ""]
        for name, items in bridges.items():
            lines.append(f"- **{name}**: " + "; ".join(items))
        lines.append("")
    if mountpoints:
        lines += ["## Mountpoints", "",
                  "Clients on different mountpoints never see each other's topics on VerneMQ. On ConnectMQ, prefix each "
                  f"mountpoint's topics (`{args.mountpoint_prefix}`) via the bridge's remote prefix, or give each "
                  "mountpoint its own ConnectMQ broker. ACL rules must cover the prefixed topics.", ""]
    lines += ["## Access control", "",
              f"{len(rules)} ConnectMQ rules generated -> `acl-rules.md`." if rules else "No ACL rules found.", ""]
    if notes or problems:
        lines += ["### Notes", ""] + [f"- {n}" for n in notes + problems] + [""]
    lines += ["## Next steps", "",
              "1. Fill `users.csv` new_username after issuing ConnectMQ credentials, then re-run with --user-map.",
              "2. Export sessions (`vmq-admin session show --client_id --user`) and build the client map with "
              "`client_id_map.py`.",
              "3. Sample `vmq-admin metrics show` for 24 h (see the planner skill) to size the plan.",
              "4. Resolve every blocker before scheduling the first wave.", ""]
    with open(os.path.join(args.out, "audit.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {args.out}/audit.md ({sum(len(v) for v in findings.values())} findings, "
          f"{len(findings['blocker'])} blockers), {len(rules)} ACL rules, {len(users)} users")
    return 0


if __name__ == "__main__":
    sys.exit(main())
