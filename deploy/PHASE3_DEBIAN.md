# Phase 3 — Debian activation

This guide activates the reviewed read-only viewer on the Debian host. The
repository remains the source of truth. Host-specific data, ACLs, service
state, and Tailscale state remain on Debian and must not be committed.

The viewer is a separate process:

- FastMCP continues on `127.0.0.1:8000`.
- The viewer listens only on `127.0.0.1:8001`.
- The existing Caddy listener and public Funnel are not changed.
- Tailscale Serve is added only after the local service is healthy.

Run one section at a time. If a command prints an error, stop and inspect it
before continuing. Do not replace `git pull --ff-only` with a forced reset.

## 1. Pull the reviewed repository

Run as the account that owns `/opt/services/linguamcp`:

```bash
cd /opt/services/linguamcp
git status --short
git fetch origin
git pull --ff-only origin main
git rev-parse --short HEAD
```

The working tree should be clean before and after the pull. The final commit
must contain `viewer/`, `tests/test_viewer.py`, `requirements.txt`, this guide,
and `deploy/systemd/linguamcp-viewer.service`.

## 2. Check the host before changing it

```bash
test -x /opt/services/linguamcp/.venv/bin/python
test -d /var/lib/linguamcp
namei -l /opt/services/linguamcp/.venv/bin/python
namei -l /var/lib/linguamcp
command -v setfacl || true
sudo systemctl status --no-pager linguamcp.service
sudo tailscale serve status
sudo tailscale funnel status
```

The existing `linguamcp.service` should remain active. The Serve and Funnel
status output is a baseline; do not run `tailscale serve reset`.

## 3. Install viewer dependencies

Use the repository's existing virtual environment. This does not restart
FastMCP:

```bash
cd /opt/services/linguamcp
/opt/services/linguamcp/.venv/bin/python -m pip install -r requirements.txt
/opt/services/linguamcp/.venv/bin/python -m pip check
```

## 4. Create the dedicated service user

Create the account only if it does not already exist:

```bash
if ! getent passwd linguamcp-viewer >/dev/null; then
  sudo useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin linguamcp-viewer
fi
getent passwd linguamcp-viewer
id linguamcp-viewer
```

The final shell should be `/usr/sbin/nologin`. The account must not be added
to the `linguamcp` group or any administrator group.

## 5. Grant read-only access to learner data

The `acl` package provides named-user ACLs. Install it if the preflight did
not find `setfacl`:

```bash
if ! command -v setfacl >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y acl
fi
```

Apply read/traverse access to existing directories and files. The default ACL
on every directory is inherited by future learner files and subdirectories.
The writer keeps that inherited read access effective on new visible Markdown
files; private recovery files remain owner-only:

```bash
sudo setfacl -m u:linguamcp-viewer:--x /var/lib/linguamcp
sudo find /var/lib/linguamcp -type d -exec setfacl -m u:linguamcp-viewer:r-x,d:u:linguamcp-viewer:r-x {} +
sudo find /var/lib/linguamcp -type f -exec setfacl -m u:linguamcp-viewer:r-- {} +
```

Inspect only the ACL entries, not the Markdown contents:

```bash
sudo getfacl -p /var/lib/linguamcp | sed -n '1,24p'
```

The `linguamcp-viewer` entry on directories must be `r-x`, and on files it
must be `r--`. There must be no `w` in that user's ACL entry.

Files created before the inherited-ACL write fix may still list in the viewer
but fail to open. For each affected visible Markdown file, verify the viewer
cannot read it and then restore only its read permission, for example:

```bash
sudo -u linguamcp-viewer test -r /var/lib/linguamcp/german/current-lesson-contract.md || \
  sudo setfacl -m u:linguamcp-viewer:r-- /var/lib/linguamcp/german/current-lesson-contract.md
```

## 6. Verify repository access and failed writes

First verify that the service account can execute the existing environment and
read one Markdown file:

```bash
sudo -u linguamcp-viewer -- test -x /opt/services/linguamcp/.venv/bin/python
sudo -u linguamcp-viewer -- /opt/services/linguamcp/.venv/bin/python -c 'import viewer; print("viewer import: ok")'
sudo -u linguamcp-viewer -- sh -c '
  set -eu
  target=$(find /var/lib/linguamcp -type f -name "*.md" -print -quit)
  if [ -z "$target" ]; then
    echo "SKIP: no Markdown files exist yet"
    exit 0
  fi
  test -r "$target"
  printf "read: ok (%s)\n" "$target"
'
```

Then prove that create, modify, rename, and delete attempts fail. This probe
must print four `expected failure` lines and must leave learner data unchanged:

```bash
sudo -u linguamcp-viewer -- sh -c '
  set -eu
  root=/var/lib/linguamcp
  probe="$root/.linguamcp-viewer-write-probe"
  target=$(find "$root" -type f -name "*.md" -print -quit)

  if (umask 077; : >"$probe") 2>/dev/null; then
    echo "UNEXPECTED: create succeeded"
    rm -f "$probe"
    exit 1
  else
    echo "expected failure: create"
  fi

  if [ -z "$target" ]; then
    echo "SKIP: no Markdown file exists; defer modify, rename, and delete probes"
    exit 0
  fi

  if printf "%s\n" "permission probe" >>"$target" 2>/dev/null; then
    echo "UNEXPECTED: modify succeeded"
    exit 1
  else
    echo "expected failure: modify"
  fi

  if mv "$target" "$target.viewer-probe" 2>/dev/null; then
    echo "UNEXPECTED: rename succeeded"
    exit 1
  else
    echo "expected failure: rename"
  fi

  if rm "$target" 2>/dev/null; then
    echo "UNEXPECTED: delete succeeded"
    exit 1
  else
    echo "expected failure: delete"
  fi
'
```

If any write unexpectedly succeeds, stop immediately and do not start the
service.

## 7. Install and start the versioned systemd unit

Install the repository template byte-for-byte. Do not edit the copy under
`/etc`:

```bash
cd /opt/services/linguamcp
sudo install -o root -g root -m 0644 \
  deploy/systemd/linguamcp-viewer.service \
  /etc/systemd/system/linguamcp-viewer.service
sha256sum deploy/systemd/linguamcp-viewer.service /etc/systemd/system/linguamcp-viewer.service
sudo systemd-analyze verify /etc/systemd/system/linguamcp-viewer.service
sudo systemctl daemon-reload
sudo systemctl enable --now linguamcp-viewer.service
sudo systemctl status --no-pager linguamcp-viewer.service
```

The two hashes must match. A healthy service should show `active (running)`.
If it fails, collect the following without exposing document content:

```bash
sudo journalctl -u linguamcp-viewer.service -n 80 --no-pager
sudo systemctl cat linguamcp-viewer.service
```

## 8. Test the local listener

The listener must be loopback-only:

```bash
curl --fail --silent --show-error http://127.0.0.1:8001/healthz
curl --fail --silent --show-error http://127.0.0.1:8001/api/languages
sudo ss -ltnp | grep ':8001'
```

The `ss` output must show `127.0.0.1:8001`, not `0.0.0.0:8001` or
`[::]:8001`. The health response is expected to be:

```json
{"status":"ok"}
```

## 9. Inspect Tailscale identity and existing exposure

Do this before changing Serve or the tailnet policy:

```bash
sudo tailscale serve status
sudo tailscale funnel status
sudo tailscale status --self
sudo tailscale status --json | python3 -c '
import json, sys
data = json.load(sys.stdin)
self_node = data.get("Self", {})
user_id = str(self_node.get("UserID", ""))
users = data.get("User", {})
user = users.get(user_id, {}) if isinstance(users, dict) else {}
print(json.dumps({
    "hostName": self_node.get("HostName"),
    "dnsName": self_node.get("DNSName"),
    "tailscaleIPs": self_node.get("TailscaleIPs"),
    "userID": self_node.get("UserID"),
    "loginName": user.get("LoginName"),
}, indent=2))
'
```

Use the discovered `loginName` as the owner identity and the server's
Tailscale IPv4 address from `tailscaleIPs` as the destination. Use `dnsName`
later as the browser URL. Do not guess any of these values. In the Tailscale
admin policy, add a narrow rule for only that user to only this server on TCP
8443. Preserve the rest of the existing policy; do not replace an existing
`grants` or `acls` section. If the tailnet currently grants all users access
to all ports, that broad rule must be narrowed before this can be considered
restricted.

For a grants-based policy, the network capability has the following shape
(merge it into the existing policy and substitute the discovered values):

```json
{
  "src": ["OWNER_LOGIN_NAME"],
  "dst": ["SERVER_TAILSCALE_IP"],
  "ip": ["tcp:8443"]
}
```

For a legacy ACL-based policy, the equivalent rule is:

```json
{
  "action": "accept",
  "src": ["OWNER_LOGIN_NAME"],
  "proto": "tcp",
  "dst": ["SERVER_TAILSCALE_IP:8443"]
}
```

Do not apply either example until the current policy format and existing
allow-rules have been inspected. A policy change belongs in the Tailscale
admin policy, not in this repository.

## 10. Add private Serve only after the local checks pass

Confirm that port 8443 is not already used by the current Serve configuration,
then run:

```bash
sudo tailscale serve --bg --https=8443 8001
sudo tailscale serve status
```

This adds the viewer as a private tailnet HTTPS listener. Do not change the
existing port-443 Funnel and do not add the viewer to Caddy.

From the authorized tailnet device, open the HTTPS URL shown by
`tailscale serve status` and check that the language picker and a Markdown
document load. From a non-authorized tailnet identity, the same URL must be
blocked. From the public Funnel URL, the viewer must not be reachable.

## Rollback

Rollback does not touch learner data:

```bash
sudo tailscale serve --https=8443 off
sudo systemctl disable --now linguamcp-viewer.service
```

Keep the unit file installed for inspection unless a later repository change
replaces it. To restore a future version, pull the repository's reviewed unit,
install it again, run `systemd-analyze verify`, reload systemd, and restart
only `linguamcp-viewer.service`.
