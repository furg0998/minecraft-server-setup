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
                          │   itzg/minecraft-bedrock-server             │
                          │   TCP 19132 (NetherNet signaling)           │
                          │   UDP 19140-19155 (one per player)          │
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
`-MaxPlayers`, `-EnableAutoStop`, `-IdleShutdownMinutes`, `-Region`,
`-StackName`.

## Important: access control is the allow-list, not a password

Bedrock Dedicated Server has **no server password feature**. Access is
controlled by the allow-list (`allow-list=true` is enforced by modern BDS
versions): only the gamertags in `AllowedPlayers` can join, and only with a
signed-in Microsoft/Xbox account (`ONLINE_MODE=true`). The *panel* password only
protects the start/stop website — never share it with people who shouldn't
control the server.

For the most reliable allow-listing use `gamertag:XUID` pairs:

- Look up XUIDs at <https://www.cxkes.me/xbox/xuid>
- Or check the server logs — a player's XUID is printed on join:
  `aws logs filter-log-events --log-group-name /ecs/minecraft-bedrock`

To change allowed players later, redeploy with the new `-AllowedPlayers` list
(stack update). Then stop+start the server so the new task picks it up.

## Notes / limitations

- **The server IP changes every time it starts.** The panel always shows the
  current address — kids should check it before joining. (Elastic IPs can't
  attach to Fargate; an NLB for a static IP would cost ~$16/mo.)
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
| Secrets Manager (1 secret)   | $0.40/month                                 |
| VPC / public subnets         | free (no NAT gateway)                       |

A weekend of play (say 10 h/week) is roughly **$2/month**; leaving it on 24/7
would be ~$36/month — which is what the auto-stop prevents.
