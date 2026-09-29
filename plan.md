# Rotating-Host Minecraft App: Project Plan

## 1. Goal

Let a small group play one shared Minecraft world where **any member can host**, one at a time, without paying for a server. The world "baton" lives in cloud storage. Whoever hosts downloads it, plays, then uploads it back. Everyone connects peer-to-peer over a private Tailscale network.

**Non-goals (for v1):** public players who don't install the app, custom domain/DNS, mod management, more than one world, automatic host election.

## 2. Architecture

| Component | Role | Technology |
|---|---|---|
| Storage | World snapshots (versioned) | Cloudflare R2 |
| Control API | Auth, lock, host address, presigned URLs, Tailscale key minting | FastAPI on Render |
| Network | Private P2P mesh, CGNAT bypass | Tailscale (tagged nodes + ACLs) |
| Client app | Everything on the player's PC | Python, packaged with PyInstaller + installer |

**No DNS.** Guests get the host's `100.x.x.x:port` from the API.

### Data flow (one session)

```
Host app -> API: acquire lock
API -> Host app: owner_token + presigned download URL
Host app <- R2: world.zip
Host app: launch game, detect port
Host app -> API: set host address (tailscale_ip, port)
Guest app -> API: who is hosting?
Guest app: add server entry / show address, connect over Tailscale
Host app -> API: heartbeat every 30s, autosave uploads every 10-15 min
Host quits -> app uploads final world -> API verifies -> release lock
```

## 3. Key design decisions

1. **One host at a time**, enforced by a lock with owner token and TTL.
2. **Never overwrite the last good version.** Uploads go to a new versioned key. The "current" pointer only moves after the upload is verified.
3. **All secrets stay on Render** (R2 keys, Tailscale API key). The client never holds long-lived credentials.
4. **Onboarding via Tailscale user invites**, created by the API (never by the client). Each friend is a real Tailscale user (one login per person) invited through the Tailscale API. Invites are one-time use and expire if unused.
5. **Default-deny ACLs** using a `players` group: only players can reach each other's devices. Note: Open to LAN picks a random port, so port-level restriction is only possible with a fixed-port dedicated server.
6. **Host retries and timeouts everywhere** because Render's free tier cold-starts.

## 4. API specification

Base URL: Render service. All requests (except `/join`) carry the player's API token.

| Endpoint | Purpose |
|---|---|
| `POST /join` | Body: app invite code, email, display name. Verifies the code, creates the player, returns an API token, and asks Tailscale (API: create user invite) to invite that email to the tailnet as a Member. |
| `GET /join/status` | Client polls this after `/join`. Reports whether the Tailscale invite was accepted (user appears in the tailnet). |
| `POST /admin/invite-code` | Admin only. Creates a new single-use app invite code. |
| `GET /admin/players` | Admin only. Lists players and Tailscale slots used (free plan cap: 6 users including you). |
| `POST /admin/revoke` | Admin only. Revokes a player's API token and deletes a pending Tailscale invite. |
| `GET /config` | Returns min client version, required Minecraft version, current world version. |
| `POST /lock/acquire` | If free (or expired), issues `owner_token` with TTL (e.g. 90s) and a presigned download URL for the current world version. Otherwise returns 409 with the current holder's name. |
| `POST /lock/heartbeat` | Body: `owner_token`. Extends TTL. Returns 410 if the lock was lost. |
| `POST /host/address` | Body: `owner_token`, `tailscale_ip`, `port`. Stores current host address. |
| `GET /host` | Returns `{host_name, ip, port, since}` or `{hosting: false}`. |
| `POST /world/upload-url` | Body: `owner_token`, size, sha256. Returns presigned PUT URL for a new version key. |
| `POST /world/commit` | Body: `owner_token`, version key, sha256. Verifies the object exists and the checksum matches, then moves the "current" pointer. |
| `POST /lock/release` | Body: `owner_token`. Clears lock and host address. Only allowed after a successful commit (or explicit "abandon"). |

### Data model (small, can start as a JSON file or SQLite on Render, but note Render's free disk is ephemeral, so prefer a free hosted DB or store state in R2)

- **players:** id, name, token_hash, created_at, revoked
- **invites:** code_hash, uses_left, expires_at
- **lock:** holder_id, owner_token_hash, expires_at
- **host:** ip, port, updated_at
- **world_versions:** key, sha256, size, created_by, created_at, is_current

### R2 layout

```
worlds/<version_id>.zip     # every upload, kept last N (e.g. 10)
```
Enable a lifecycle rule or a cleanup job to prune old versions.

## 5. Client app modules

1. **installer:** silently installs Tailscale (one admin/UAC prompt), the app, and a shortcut. Detects TLauncher; does not bundle it. Runs `tailscale login` to open the browser sign-in.
2. **auth / onboarding wizard:** first run asks for app invite code and email, calls `/join`, stores the API token in Windows Credential Manager, then walks the friend through: install Tailscale, sign in (Google/Microsoft/GitHub), accept the Tailscale invite from the email. The app polls `tailscale status` and `/join/status` and continues automatically once the device is on the tailnet.
2b. **admin panel (your app only, hidden for others):** generate app invite codes, see players and slots used (x/6), revoke access.
3. **api_client:** wrapper with timeouts, retries with backoff, and a "waking up server..." message for cold starts.
4. **world_sync:** download and unpack; pack (exclude cache, logs, crash reports), sha256, upload, commit. Resumable if possible.
5. **game_launcher:** launches TLauncher with the pinned profile; finds the `javaw` process; detects exit.
6. **lan_sniffer:** joins multicast group `224.0.2.60:4445` on the correct interface (the physical adapter, not Tailscale), parses the port from the announcement. Fallback: manual port entry field.
7. **heartbeat and autosave:** background thread; on lock loss, warn the user immediately.
8. **guest_connect:** fetches `/host`; writes a server entry to `servers.dat` (using `nbtlib`) or shows the address with a copy button.
9. **updater:** checks `/config`; blocks use if the client is older than the minimum version.
10. **ui:** simple window (Tkinter or PySide). Buttons: Host, Join, Hand off. Status line and log panel.

## 6. Phases with acceptance tests

Do these in order. Don't start a phase until the previous test passes.

### Phase 0: Network feasibility (half a day)
- Install Tailscale on two PCs on different networks (e.g. hostel Wi-Fi and a phone hotspot).
- Host a LAN world, connect from the other PC via `100.x.x.x:port`.
- **Pass if:** connection works. Check `tailscale status` to see whether it's direct or relayed, and note the lag.
- **If it fails or is too laggy:** reconsider (relay VPS, dedicated server approach).

### Phase 1: API skeleton
- FastAPI app with lock acquire, heartbeat, release, host address, and `/host`. In-memory or SQLite state.
- **Pass if:** two clients cannot hold the lock at once; the lock expires if heartbeats stop; a wrong owner token is rejected.

### Phase 2: Storage and world sync
- R2 bucket, presigned URLs, versioned uploads, commit with checksum.
- CLI script to download, unpack, pack, upload.
- **Pass if:** a kill mid-upload leaves the previous version intact; a corrupted upload is rejected at commit.

### Phase 3: Client host flow
- Combine lock, download, launch, sniff port, post address, heartbeat, exit detection, upload, release.
- **Pass if:** a full host session works end to end, including force-killing the game and the app (lock expires, next host gets the last good version).

### Phase 4: Guest flow
- Fetch host, add server entry, connect.
- **Pass if:** a second PC joins with no manual typing (or one copy-paste).

### Phase 5: Onboarding, invites, and access control
- App invite codes, `/join`, Tailscale user-invite creation through the API (OAuth client, not a short-lived token), `/join/status` polling, admin endpoints, ACL policy file with a `players` group.
- **Pass if:** a new friend can go from installer to connected using only an app code, an email, and one sign-in; a used or expired app code fails; a revoked player is rejected; a non-player device cannot reach any player's PC.

### Phase 6: Hardening
- Autosave, host handoff warning, version checks, better error messages, log file, rate limiting on the API.
- **Pass if:** the failure list in section 7 is handled.

### Phase 7: Packaging
- PyInstaller build, installer (Inno Setup or similar), test on a clean Windows machine, handle SmartScreen and antivirus warnings (document the "run anyway" steps if not code-signed).

## 6A. Local world conflict rules

The app keeps one fixed world folder (e.g. `OurWorld`) in the TLauncher singleplayer saves path. It only touches this folder while Minecraft is closed, and always unpacks into a temp folder first, then swaps it in.

It stores a **last-synced marker** locally: the version ID plus a hash of the world folder, and a flag `upload_pending` that is set when a hosting session starts and cleared only after `/world/commit` succeeds.

Before every host or join-prep step, compare the local folder with the marker:

| Situation | Action |
|---|---|
| Local world matches the last synced version | Safe to override with the latest cloud version |
| `upload_pending` is set (last session never committed) | Retry the upload first, or ask the player "Your last session wasn't saved to the cloud. Upload it now?" Only after a successful commit, download the latest |
| Local world differs and `upload_pending` is not set (someone played solo) | Back up the local world, then override with the shared one |

**Backups**
- Stored in the app's own folder, **not** inside `saves`, so they don't clutter the Singleplayer list.
- Named like `OurWorld_overridden_2026-09-29_2130`.
- Only created when the local world actually differs from the marker.
- Keep the last 3-5, delete older ones.
- Tell the player: "Local changes found and saved to backups. Loading the shared world."

**Must-test:** kill the app mid-upload, then restart. The next session must retry the upload and must never download over the only good copy.

## 6B. Admin authentication

Admin rights are decided by the **server only**. Nothing in the client exe can be trusted, since anyone can edit it.

- `ADMIN_SECRET`: a long random value set as an environment variable on Render. Never in git, never in the exe, never pasted into an AI tool or chat.
- In the admin's copy of the app, the hidden admin panel asks for the secret once and stores it in Windows Credential Manager.
- Every `/admin/*` request sends it in a header (never in the URL, which gets logged) together with the admin's normal player token.
- The server compares the secret in constant time (`hmac.compare_digest`). A wrong or missing secret returns 403.
- Other players' apps contain no admin secret, so admin calls from them always fail.
- Rate limit `/admin/*` and lock out after repeated wrong attempts.
- Log every admin action (invite code created, player revoked, invite deleted).
- To rotate: change the environment variable on Render and re-enter the secret in the admin app.
- Later option: replace the shared secret with an `is_admin` flag per player stored on the server, if co-admins are needed.

**Never:** hardcode the secret in the client, check `is_admin` locally, or reuse the player token as the admin credential.

## 7. Failure cases to test

| Scenario | Expected behaviour |
|---|---|
| Host app crashes mid-session | Lock expires; next host loads the last autosave |
| Host PC loses internet | Heartbeat fails; app warns; lock expires after TTL |
| Upload fails at the end | App retries; keeps the local copy; lock is not released until commit succeeds |
| Two players click Host at once | One gets 409 with the holder's name |
| Render is asleep | Client shows a waiting message and retries with backoff |
| Guest has wrong Minecraft version | App blocks with a clear message |
| Old client version | API refuses; app prompts for update |
| Port sniffing fails | App offers manual port entry |
| Host leaves without handing off | Everyone is disconnected; autosave protects most progress |

## 8. Security checklist

- No R2 or Tailscale API credentials in the client, ever.
- Store only hashes of invite codes and player tokens on the server.
- Owner token is required for every lock or world write.
- Presigned URLs are short-lived (a few minutes) and scoped to a single key.
- Tailscale API credentials live only on Render, ideally as an OAuth client limited to user invites, so nothing expires every 1-90 days.
- App invite codes are single-use and expire; admin endpoints require a separate admin token.
- One Tailscale login per person (accounts may not be shared).
- ACLs deny by default.
- Rate limit `/join` and lock endpoints.
- Note TLauncher uses offline-mode accounts, so names are not authenticated inside the game. Rely on the invite-only tailnet as the access control.

## 9. Known risks and open questions

1. **Relayed Tailscale traffic on hostel/campus networks:** tested in Phase 0.
2. **Tailscale free plan user limit:** check current limits against your group size; fallback is Headscale or node sharing.
3. **World size:** if zips get large, sync only changed region files (later).
4. **Multicast sniffing on Windows with several adapters:** may need interface selection logic or a manual fallback.
5. **Dedicated server alternative:** running a real server jar on the host with a fixed port removes sniffing entirely. Consider switching if Phase 3 sniffing is flaky.
6. **Render free tier:** cold starts and ephemeral disk. Keep state in R2 or a free hosted DB.
7. **TLauncher is unofficial:** don't redistribute it; consider supporting the official launcher too.
8. **Antivirus flags on PyInstaller exes:** expect them; an installer and code signing help.

## 10. Working with a coding assistant

- Give this file as context, then request **one phase or one module at a time**.
- Ask it to write tests for the lock and world-sync logic, and read that code yourself.
- Commit to git after each working step.
- Put secrets in environment variables on Render; keep a `.env.example` in the repo and `.env` in `.gitignore`.
- Suggested repo layout:

```
/server        FastAPI app, tests
/client        Python app modules, UI
/installer     Installer script, build config
/docs          plan.md, ACL policy, setup notes
```

## 11. Definition of done (v1)

Five players install the app with an invite code. Any of them can host, the others join within a minute, the world survives a host crash with at most 15 minutes of lost progress, and no one can access anything on anyone else's PC beyond the Minecraft port.
