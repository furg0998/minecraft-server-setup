# Minecraft Bedrock Server — On-Demand on AWS

A CloudFormation stack that runs a Minecraft **Bedrock** dedicated server on ECS
Fargate with persistent world storage on EFS. The server only costs money while
it is running. A small web panel (hosted for free on an `execute-api` URL —
no domain registration needed) lets the kids start/stop the server, see the
current address, and see who is playing.

## Architecture

```
 Kid's browser                        AWS
 ──────────────   HTTPS   ┌─────────────────────────────────────────────┐
    panel  ──────────────►│ API Gateway (HTTP API, free *.execute-api    │
    (start/stop,          │  domain) → Lambda (Python)                  │
     status, players)     │   ├── GET  /           → static SPA          │
                          │   ├── GET  /api/status → ECS/EC2/Logs lookup │
                          │   └── POST /api/control→ ECS desiredCount   │
                          │        1/0                                   │
                          │ EventBridge ── every 15 min ──► auto-stop   │
                          │   (stops server if empty > IdleShutdown)    │
                          │                                             │
                          │ ECS Fargate task (only runs when desired=1) │
                          │   ├─ itzg/minecraft-bedrock-server          │
                          │   │   TCP 19132 (NetherNet signaling)       │
                          │   │   UDP 19140-19155 (one per player)      │
                          │   ├─ map-render (uNmINeD, every 10 min)     │
                          │   └─ map-web (nginx) → http://<ip>/ on :80  │
                          │        │                                    │
                          │        ▼                                    │
                          │ EFS (retained): /data = binaries + world    │
                          └─────────────────────────────────────────────┘
```

## Deploy

Prereqs: AWS CLI installed and authenticated (`aws configure` / `aws login`).

```powershell
.\deploy.ps1 -AllowedPlayers "Steve:2533274790567123,Alex:2535412345678901" `
             -PanelPassword "pick-a-password"
```

Or let it generate the panel password for you (retrieve it from the
`PanelSecret` output afterwards):

```powershell
.\deploy.ps1 -AllowedPlayers "Steve:2533274790567123,Alex:2535412345678901"
```

The script creates an artifacts bucket, runs `aws cloudformation package`, then
`aws cloudformation deploy`. When it finishes it prints the **PanelUrl** — give
that URL and the panel password to the kids.

Other `deploy.ps1` parameters: `-ServerName`, `-GameMode`, `-Difficulty`,
`-MaxPlayers`, `-EnableAutoStop`, `-IdleShutdownMinutes`, `-EnableAllowList`,
`-EnableMap`, `-HostedZoneId`, `-HostedZoneName`, `-DnsPrefix`,
`-EnableAccessPortal`, `-AdminEmail`, `-Region`, `-StackName`.

**Every `deploy.ps1` run must re-pass all parameters you care about** —
anything not passed resets to its template default. To make this easy, put your
values in `deploy.local.json` (gitignored — it stays on your machine and is
never committed):

```json
{
  "AllowedPlayers": "furgle7287:2535418409774460",
  "PanelPassword": "pokemon",
  "EnableAllowList": "true",
  "EnableMap": "true"
}
```

The script uses the file as defaults; explicit command-line parameters still
win for one-off overrides.

## World map

While the server is running, a uNmINeD-rendered interactive map of the world is
served by an nginx sidecar at `http://<server-ip>/` (port 80). The panel shows
an "Open world map" button when it's available.

- The renderer sidecar snapshots `worlds/` from EFS and re-renders every 10
  minutes (`MAP_INTERVAL_SECONDS` in the task definition).
- Tiles persist on the EFS `/map` access point, so re-renders are incremental.
- First render takes a few minutes after startup (uNmINeD CLI is downloaded to
  `/data/.tools` on first boot, then cached).
- The map is only reachable while the server is running — same public IP.
- Disable with `-EnableMap false` if you want the task's CPU/memory purely for
  the game (rendering is `nice`'d, but still shares the task).

## Important: access control is the allow-list, not a password

Bedrock Dedicated Server has **no server password feature**. Access is
controlled by the allow-list (`allow-list=true` is enforced by modern BDS
versions): only the gamertags in `AllowedPlayers` can join, and only with a
signed-in Microsoft/Xbox account (`ONLINE_MODE=true`). The *panel* password only
protects the start/stop website — never share it with people who shouldn't
control the server.

For the most reliable allow-listing use `gamertag:XUID` pairs:

- Look up XUIDs at <https://rghlab.co.uk/xuid/> (the panel can also look them
  up automatically — see "Access requests" below)
- Or check the server logs — a player's XUID is printed on join:
  `aws logs filter-log-events --log-group-name /ecs/minecraft-bedrock`

The allow-list lives in a **Secrets Manager secret** (`player-access`), injected
into the container at task start. It holds four keys:

| Key | Owner |
|---|---|
| `players` | the `-AllowedPlayers` deploy parameter (rewritten on each deploy) |
| `approved` | the admin approval page (preserved across deploys) |
| `requests` | pending access requests from the panel form |
| `combined` | `players` + `approved` — what the container actually reads |

A CloudFormation custom resource (`MergeAllowList`) merges them on each deploy,
so **admin approvals survive redeploys**. Removing a player:

- from `deploy.local.json`/`-AllowedPlayers` works normally;
- admin-approved players must be removed via Secrets Manager (edit the
  `approved` key) or the admin page.

Approving a player updates the secret directly — no CloudFormation deploy
needed — and **restarts the server task** (~1–2 min) if it's running, since
`allowlist.json` is written at container start and can't be hot-reloaded.

To disable the allow-list entirely (anyone with the IP can join), redeploy with
`-EnableAllowList false`. Prefer keeping it on — the allow-list is the only
per-player access control Bedrock has.

## Access requests + admin approval

When `EnableAccessPortal` is true (default), the panel has a **"Need access?"**
form: a kid types their gamertag, the Lambda looks up the XUID via
`api.rghlab.co.uk`, and stores a pending request in the `player-access` secret.

You review them on the admin page — `admin.<prefix>.<zone>` with a hosted zone,
or `<execute-api-url>/admin` without — which is protected by a **Cognito
user pool** (invite-only, AWS-hosted login). Create your admin user at deploy:

```powershell
.\deploy.ps1 -AdminEmail "you@example.com"
```

Cognito emails a temporary password; use it to sign in at the admin URL. The
page shows pending requests (with a warn banner if the server is running) and
the current allow-list; **Approve** writes the player into the secret and
restarts the task if needed. The Lambda role for this is scoped to
`secretsmanager:PutSecretValue` on that one secret plus `ecs:UpdateService` on
the service — it cannot touch the rest of the stack.

## Custom domain + HTTPS (optional)

If you own a Route53 hosted zone in the same account, set `HostedZoneId`,
`HostedZoneName` and (optionally) `DnsPrefix` — in `deploy.local.json` or via
`-HostedZoneId`/`-HostedZoneName`/`-DnsPrefix`. The stack then creates three
records under `server.|panel.|map.<prefix>.<zone>` (prefix defaults to the
stack name):

| Hostname | What it does |
|---|---|
| `server.<prefix>.<zone>` | A record the Lambda repoints to the task's public IP on every start (via ECS state-change events; deleted on stop). Kids always connect to the same name — no more changing IP. |
| `panel.<prefix>.<zone>` | HTTPS custom domain on the same API Gateway → the control panel. |
| `map.<prefix>.<zone>` | HTTPS custom domain → Lambda proxies the rendered tiles from a private S3 bucket. Works **even when the server is off** (tiles persist). |
| `admin.<prefix>.<zone>` | HTTPS custom domain → the Cognito-protected admin approval page. |

An ACM certificate (free, auto-validated through the hosted zone) covers the
panel and map domains. No Elastic IP, NLB or NAT is needed — the game needs
direct UDP/TCP anyway, and the web services all ride the API Gateway.

When no hosted zone is configured everything still works with the raw
execute-api URL and `http://<ip>/` for the map.

## Notes / limitations

- **The server IP changes every time it starts.** With a hosted zone
  configured, `server.<prefix>.<zone>` tracks it automatically (60s TTL).
  Without one, the panel always shows the current IP.
- **Ports**: TCP 19132 + UDP 19140–19155 must be reachable. Bedrock 1.26.50+
  uses NetherNet; the old UDP-only RakNet transport is being removed by Mojang.
- **Startup takes ~1–2 minutes** (Fargate provisioning + BDS boot; first boot
  also downloads the server binary into EFS, later boots are faster).
- **Auto-stop**: every 15 min a check stops the server if it has had zero
  players for `IdleShutdownMinutes` (default 45). Disable with
  `-EnableAutoStop false`.
- **Updates stop the server**: the template pins `DesiredCount: 0`, so any
  stack update resets it off — start it again from the panel afterwards.
- **Deleting the stack keeps the world**: the EFS file system has
  `DeletionPolicy: Retain`. Delete it manually in the console if you truly want
  the world gone.
- **Panel served by API Gateway/Lambda** (same origin, no CORS, one URL).
  If you'd rather host the SPA on S3 + CloudFront, `lambda/index.html` is a
  fully static file — just swap `window.location` fetches for the API URL.

## Cost (rough, us-east-1)

| Thing                        | Cost                                        |
|------------------------------|---------------------------------------------|
| Fargate 1 vCPU / 2 GB task   | ~$0.049/hour, only while running            |
| EFS (world + binaries)       | ~$0.30/GB-month (a few GB → pennies)        |
| Lambda + API Gateway         | effectively free at this volume             |
| Secrets Manager (2 secrets)  | $0.80/month                                 |
| Cognito (admin logins)       | free (well under the 10k MAU free tier)     |
| VPC / public subnets         | free (no NAT gateway)                       |

A weekend of play (say 10 h/week) is roughly **$2/month**; leaving it on 24/7
would be ~$36/month — which is what the auto-stop prevents.
