#!/usr/bin/env python3
"""Discord server management CLI for Claude.

Auth: the bot token is read from Bitwarden via `rbw get <item>` and never printed.
Transport: Discord REST API v10. A User-Agent header is REQUIRED — urllib's default
UA gets a Cloudflare 1010 block before Discord ever sees the request.

Resolve a target with --server <name> (looks up ~/.config/chadacys_discord/servers.json
for {guild, token_item}) or pass --guild and --token-item directly.

Examples:
  discord.py inventory --server chadacys
  discord.py category --server chadacys --name "NEW CATEGORY" --private
  discord.py channel  --server chadacys --name new-chan --parent <cat_id> --type text
  discord.py role     --server chadacys --name Supporter --color 14917658 --hoist
  discord.py topic    --server chadacys --channel welcome --text "Start here."
  discord.py perms    --server chadacys --channel tw-beta --role Playtester --allow 1024
  discord.py post     --server chadacys --channel announcements --content "..." [--pin]
"""
import argparse, base64, json, os, subprocess, sys, time, urllib.request, urllib.error

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://chadacys.dev, 1.0)"
VIEW_CHANNEL = 1 << 10  # 1024
REGISTRY = os.path.expanduser("~/.config/chadacys_discord/servers.json")
CTYPE = {"text": 0, "voice": 2, "category": 4, "announce": 5, "forum": 15}


def load_registry():
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except Exception:
        return {}


def get_token(item):
    # rbw get returns the password field (the bot token). Never printed.
    return subprocess.check_output(["rbw", "get", item]).decode().strip()


def req(token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bot " + token,
        "User-Agent": UA,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(r) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        if e.code == 429:
            try:
                retry = float(json.loads(txt).get("retry_after", 1))
            except Exception:
                retry = 1.0
            time.sleep(retry + 0.5)
            return req(token, method, path, body)
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"error": txt}


def resolve(args):
    reg = load_registry()
    guild, token_item = args.guild, args.token_item
    if args.server:
        s = reg.get(args.server)
        if not s:
            sys.exit("unknown --server '%s' (known: %s)" % (args.server, ", ".join(reg) or "none"))
        guild = guild or s.get("guild")
        token_item = token_item or s.get("token_item")
    if not guild and args.cmd not in ("me", "guilds"):
        sys.exit("need --guild or a --server that maps to one")
    if not token_item:
        token_item = "Treasure Wake Discord Bot"
    return get_token(token_item), guild


def chan_id(token, G, name):
    if str(name).isdigit():
        return name
    _, ch = req(token, "GET", "/guilds/%s/channels" % G)
    for c in ch:
        if c["name"] == name:
            return c["id"]
    sys.exit("no channel named '%s'" % name)


def role_id(token, G, name):
    if str(name).isdigit():
        return name
    _, rs = req(token, "GET", "/guilds/%s/roles" % G)
    for r in rs:
        if r["name"] == name:
            return r["id"]
    sys.exit("no role named '%s'" % name)


def cmd_inventory(a):
    token, G = resolve(a)
    s, g = req(token, "GET", "/guilds/%s" % G)
    print("guild:", g.get("name", g), "| id:", G)
    _, ch = req(token, "GET", "/guilds/%s/channels" % G)
    if isinstance(ch, dict):
        sys.exit("error: %s" % ch)
    cats = {c["id"]: c["name"] for c in ch if c["type"] == 4}
    for c in sorted(ch, key=lambda x: x.get("position", 0)):
        if c["type"] == 4:
            print("[%s]  id=%s" % (c["name"], c["id"]))
    for c in sorted(ch, key=lambda x: x.get("position", 0)):
        if c["type"] != 4:
            tn = {0: "text", 2: "voice", 5: "announce", 15: "forum"}.get(c["type"], str(c["type"]))
            print("  %-7s #%-22s id=%s  in=%s" % (tn, c["name"], c["id"], cats.get(c.get("parent_id"), "-")))
    _, rs = req(token, "GET", "/guilds/%s/roles" % G)
    print("roles (high->low):")
    for r in sorted(rs, key=lambda x: -x.get("position", 0)):
        print("  pos %3d  %-22s id=%s %s" % (r.get("position", 0), r["name"], r["id"], "managed" if r.get("managed") else ""))


def cmd_me(a):
    token, _ = resolve(a)
    print(req(token, "GET", "/users/@me")[1])


def cmd_guilds(a):
    token, _ = resolve(a)
    _, d = req(token, "GET", "/users/@me/guilds")
    for g in (d if isinstance(d, list) else []):
        print(g["id"], g["name"])


def cmd_category(a):
    token, G = resolve(a)
    body = {"name": a.name, "type": 4}
    if a.private:
        body["permission_overwrites"] = [{"id": G, "type": 0, "deny": str(VIEW_CHANNEL)}]
    print(*req(token, "POST", "/guilds/%s/channels" % G, body))


def cmd_channel(a):
    token, G = resolve(a)
    body = {"name": a.name, "type": CTYPE.get(a.type, 0)}
    if a.parent:
        body["parent_id"] = a.parent
    print(*req(token, "POST", "/guilds/%s/channels" % G, body))


def cmd_role(a):
    token, G = resolve(a)
    body = {"name": a.name, "mentionable": a.mentionable, "hoist": a.hoist}
    if a.color is not None:
        body["color"] = a.color
    print(*req(token, "POST", "/guilds/%s/roles" % G, body))


def cmd_delete_role(a):
    token, G = resolve(a)
    print(*req(token, "DELETE", "/guilds/%s/roles/%s" % (G, role_id(token, G, a.role))))


def cmd_delete_channel(a):
    token, G = resolve(a)
    print(*req(token, "DELETE", "/channels/%s" % chan_id(token, G, a.channel)))


def cmd_topic(a):
    token, G = resolve(a)
    print(*req(token, "PATCH", "/channels/%s" % chan_id(token, G, a.channel), {"topic": a.text}))


def cmd_perms(a):
    token, G = resolve(a)
    cid = chan_id(token, G, a.channel)
    oid = role_id(token, G, a.role)
    print(*req(token, "PUT", "/channels/%s/permissions/%s" % (cid, oid),
               {"type": 0, "allow": str(a.allow or 0), "deny": str(a.deny or 0)}))


def cmd_post(a):
    # NOTE: posting to a shared server is an EXTERNAL action — see SKILL.md.
    content = open(a.file).read() if a.file else a.content
    token, G = resolve(a)
    cid = chan_id(token, G, a.channel)
    s, d = req(token, "POST", "/channels/%s/messages" % cid, {"content": content})
    print("post", s, "msg", d.get("id", d))
    if a.pin and s in (200, 201):
        print("pin", req(token, "PUT", "/channels/%s/pins/%s" % (cid, d["id"]))[0])


def cmd_rename_server(a):
    token, G = resolve(a)
    print(*req(token, "PATCH", "/guilds/%s" % G, {"name": a.name}))


def _data_uri(path):
    with open(path, "rb") as f:
        raw = f.read()
    mime = "image/gif" if raw[:6] in (b"GIF87a", b"GIF89a") else ("image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png")
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


def cmd_icon(a):
    # Set the server icon. Discord wants a base64 data URI; PNG/JPG/GIF accepted
    # (GIF only on boosted guilds). Square art recommended (shown cropped to a circle).
    token, G = resolve(a)
    print(*req(token, "PATCH", "/guilds/%s" % G, {"icon": _data_uri(a.file)}))


def cmd_banner(a):
    # Set the server banner (needs Boost Level 1+). Wide 16:9 art recommended.
    token, G = resolve(a)
    print(*req(token, "PATCH", "/guilds/%s" % G, {"banner": _data_uri(a.file)}))


def cmd_emoji(a):
    # Upload a custom emoji. Image must be <=256KB, square; 128x128 recommended.
    # Name: 2-32 chars, alphanumeric + underscore. Roles empty = available to everyone.
    token, G = resolve(a)
    print(*req(token, "POST", "/guilds/%s/emojis" % G,
               {"name": a.name, "image": _data_uri(a.file), "roles": []}))


def cmd_invite(a):
    token, G = resolve(a)
    cid = chan_id(token, G, a.channel)
    s, d = req(token, "POST", "/channels/%s/invites" % cid,
               {"max_age": a.max_age, "max_uses": a.max_uses, "temporary": False, "unique": True})
    if s in (200, 201):
        print("https://discord.gg/%s" % d["code"])
    else:
        print("ERR", s, d)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server"); common.add_argument("--guild"); common.add_argument("--token-item")

    p = argparse.ArgumentParser(description="Discord management CLI (auth via rbw)", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name):
        return sub.add_parser(name, parents=[common])

    add("inventory").set_defaults(fn=cmd_inventory)
    add("me").set_defaults(fn=cmd_me)
    add("guilds").set_defaults(fn=cmd_guilds)

    sp = add("category"); sp.add_argument("--name", required=True); sp.add_argument("--private", action="store_true"); sp.set_defaults(fn=cmd_category)
    sp = add("channel"); sp.add_argument("--name", required=True); sp.add_argument("--parent"); sp.add_argument("--type", default="text"); sp.set_defaults(fn=cmd_channel)
    sp = add("role"); sp.add_argument("--name", required=True); sp.add_argument("--color", type=int); sp.add_argument("--hoist", action="store_true"); sp.add_argument("--mentionable", action="store_true"); sp.set_defaults(fn=cmd_role)
    sp = add("delete-role"); sp.add_argument("--role", required=True); sp.set_defaults(fn=cmd_delete_role)
    sp = add("delete-channel"); sp.add_argument("--channel", required=True); sp.set_defaults(fn=cmd_delete_channel)
    sp = add("topic"); sp.add_argument("--channel", required=True); sp.add_argument("--text", required=True); sp.set_defaults(fn=cmd_topic)
    sp = add("perms"); sp.add_argument("--channel", required=True); sp.add_argument("--role", required=True); sp.add_argument("--allow", type=int, default=0); sp.add_argument("--deny", type=int, default=0); sp.set_defaults(fn=cmd_perms)
    sp = add("post"); sp.add_argument("--channel", required=True); sp.add_argument("--content"); sp.add_argument("--file"); sp.add_argument("--pin", action="store_true"); sp.set_defaults(fn=cmd_post)
    sp = add("rename-server"); sp.add_argument("--name", required=True); sp.set_defaults(fn=cmd_rename_server)
    sp = add("icon"); sp.add_argument("--file", required=True); sp.set_defaults(fn=cmd_icon)
    sp = add("banner"); sp.add_argument("--file", required=True); sp.set_defaults(fn=cmd_banner)
    sp = add("emoji"); sp.add_argument("--name", required=True); sp.add_argument("--file", required=True); sp.set_defaults(fn=cmd_emoji)
    sp = add("invite"); sp.add_argument("--channel", default="welcome"); sp.add_argument("--max-age", type=int, default=0); sp.add_argument("--max-uses", type=int, default=0); sp.set_defaults(fn=cmd_invite)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
