"""Control panel + API for the on-demand Minecraft Bedrock server.

Routes (same API Gateway endpoint):
  GET  /            -> static single-page app
  GET  /api/status  -> server state, address, online players  (needs password)
  POST /api/control -> {"action": "start"|"stop"}             (needs password)

Also handles an EventBridge scheduled event for idle auto-stop.
"""

import base64
import hmac
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION") or os.environ["AWS_DEFAULT_REGION"]
CLUSTER = os.environ["CLUSTER_NAME"]
SERVICE = os.environ["SERVICE_NAME"]
LOG_GROUP = os.environ["LOG_GROUP_NAME"]
SECRET_ARN = os.environ["PANEL_SECRET_ARN"]
IDLE_MINUTES = int(os.environ.get("IDLE_SHUTDOWN_MINUTES", "45"))
GAME_PORT = int(os.environ.get("GAME_PORT", "19132"))
MAP_ENABLED = os.environ.get("MAP_ENABLED", "true") == "true"
SERVER_NAME = os.environ.get("SERVER_NAME", "Minecraft Server")
LOG_STREAM_PREFIX = "minecraft/minecraft/"  # <awslogs-prefix>/<container>/<task-id>

ecs = boto3.client("ecs", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)
route53 = boto3.client("route53")
s3 = boto3.client("s3", region_name=REGION)

SERVER_FQDN = os.environ.get("SERVER_FQDN", "")
MAP_FQDN = os.environ.get("MAP_FQDN", "")
HOSTED_ZONE_ID = os.environ.get("HOSTED_ZONE_ID", "")
MAP_TILES_BUCKET = os.environ.get("MAP_TILES_BUCKET", "")
ACCESS_ENABLED = os.environ.get("ACCESS_ENABLED", "false") == "true"
PLAYER_ACCESS_ARN = os.environ.get("PLAYER_ACCESS_ARN", "")
XUID_API_URL = os.environ.get("XUID_API_URL", "https://api.rghlab.co.uk/xuid")
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
ADMIN_FQDN = os.environ.get("ADMIN_FQDN", "")
ADMIN_URL = os.environ.get("ADMIN_URL", "")

cognito = boto3.client("cognito-idp", region_name=REGION)

_TILE_TYPES = {
    ".html": "text/html", ".htm": "text/html", ".js": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".txt": "text/plain",
    ".xml": "application/xml", ".wasm": "application/wasm",
}

_password = None
_password_fetched_at = 0.0
_PASSWORD_TTL = 300  # re-read the secret every 5 min so rotations propagate
_html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
try:
    _admin_html = Path(__file__).with_name("admin.html").read_text(encoding="utf-8")
except OSError:
    _admin_html = "<h1>admin page not installed</h1>"
_PLAYER_RE = re.compile(r"Player (connected|disconnected):\s*([^,]+)")


def _panel_password():
    global _password, _password_fetched_at
    if _password is None or time.time() - _password_fetched_at > _PASSWORD_TTL:
        secret = secrets.get_secret_value(SecretId=SECRET_ARN)["SecretString"]
        _password = json.loads(secret)["password"]
        _password_fetched_at = time.time()
    return _password


def _authed(event):
    given = (event.get("headers") or {}).get("x-panel-password", "")
    return bool(given) and hmac.compare_digest(given, _panel_password())


def _resp(status, body, content_type="application/json"):
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-store"},
        "body": body if isinstance(body, str) else json.dumps(body),
    }


def _admin_authed(event):
    """Validate a Cognito access token presented as Authorization: Bearer."""
    if not ACCESS_ENABLED:
        return False
    auth = (event.get("headers") or {}).get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return False
    try:
        cognito.get_user(AccessToken=token)
        return True
    except ClientError:
        return False


def _access_store():
    """Read {"players": "tag:xuid,...", "requests": [...]} from Secrets Manager."""
    raw = secrets.get_secret_value(SecretId=PLAYER_ACCESS_ARN)["SecretString"]
    data = json.loads(raw or "{}")
    data.setdefault("players", "")
    data.setdefault("requests", [])
    return data


def _save_access_store(data):
    secrets.put_secret_value(
        SecretId=PLAYER_ACCESS_ARN, SecretString=json.dumps(data)
    )


def _lookup_xuid(gamertag):
    """-> {"gamertag": canonical, "xuid": str} or None. Uses api.rghlab.co.uk."""
    url = XUID_API_URL + "?" + urllib.parse.urlencode({"gamertag": gamertag.strip()})
    req = urllib.request.Request(url, headers={"User-Agent": "mc-panel/1.0"})
    with urllib.request.urlopen(req, timeout=8) as res:
        data = json.loads(res.read().decode("utf-8"))
    profiles = data.get("profiles") or []
    if not profiles:
        return None
    summary = profiles[0].get("summary") or {}
    xuid = str(summary.get("xuid_dec") or "")
    return {"gamertag": summary.get("gamertag") or gamertag, "xuid": xuid} if xuid else None


def _players_list(data):
    return [p for p in (data.get("players") or "").split(",") if p.strip()]


def _request_access(gamertag):
    if not gamertag or len(gamertag) > 32:
        return _resp(400, {"error": "invalid gamertag"})
    try:
        found = _lookup_xuid(gamertag)
    except Exception:
        return _resp(502, {"error": "xuid lookup failed - try https://rghlab.co.uk/xuid/"})
    if not found:
        return _resp(404, {"error": "gamertag not found - check spelling"})

    data = _access_store()
    entry = f"{found['gamertag']}:{found['xuid']}"
    if any(p.split(":")[-1] == found["xuid"] or
           p.split(":")[0].lower() == found["gamertag"].lower()
           for p in _players_list(data)):
        return _resp(200, {"ok": True, "alreadyAllowed": True, **found})
    data["requests"] = [r for r in data["requests"]
                        if r.get("xuid") != found["xuid"]
                        and r.get("gamertag", "").lower() != found["gamertag"].lower()]
    data["requests"].append({**found, "ts": int(time.time())})
    _save_access_store(data)
    return _resp(200, {"ok": True, "alreadyAllowed": False,
                       "entry": entry, **found})


def _admin_approve(body):
    xuid = str(body.get("xuid") or "").strip()
    gamertag = str(body.get("gamertag") or "").strip()
    if not xuid or not gamertag:
        return _resp(400, {"error": "gamertag and xuid required"})
    data = _access_store()
    players = _players_list(data)
    if not any(p.split(":")[-1] == xuid for p in players):
        players.append(f"{gamertag}:{xuid}")
        data["players"] = ",".join(players)
    data["requests"] = [r for r in data["requests"] if r.get("xuid") != xuid]
    _save_access_store(data)

    restarting = False
    if _service().get("desiredCount", 0) > 0:
        ecs.update_service(cluster=CLUSTER, service=SERVICE, forceNewDeployment=True)
        restarting = True
    return _resp(200, {"ok": True, "approved": f"{gamertag}:{xuid}",
                       "restarting": restarting})


def _admin_reject(body):
    xuid = str(body.get("xuid") or "").strip()
    data = _access_store()
    before = len(data["requests"])
    data["requests"] = [r for r in data["requests"] if r.get("xuid") != xuid]
    if len(data["requests"]) != before:
        _save_access_store(data)
    return _resp(200, {"ok": True})


def _service():
    resp = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])
    return resp["services"][0]


def _current_task():
    """Most recent task for the service, or None."""
    task_arns = []
    for wanted in ("RUNNING", "PENDING", "STOPPED"):
        page = ecs.list_tasks(
            cluster=CLUSTER, serviceName=SERVICE, desiredStatus=wanted, maxResults=10
        )
        task_arns.extend(page.get("taskArns", []))
    if not task_arns:
        return None
    tasks = ecs.describe_tasks(cluster=CLUSTER, tasks=task_arns).get("tasks", [])
    if not tasks:
        return None
    return max(tasks, key=lambda t: t.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc))


def _public_ip(task):
    for att in task.get("attachments", []):
        if att.get("type") != "ElasticNetworkInterface":
            continue
        eni_id = next(
            (d["value"] for d in att.get("details", []) if d["name"] == "networkInterfaceId"),
            None,
        )
        if not eni_id:
            continue
        eni = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
        infos = eni.get("NetworkInterfaces", [])
        if infos:
            return (infos[0].get("Association") or {}).get("PublicIp")
    return None


def _tcp_reachable(ip, port=GAME_PORT, timeout=2.5):
    try:
        socket.create_connection((ip, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _player_events(task_arn):
    """All connect/disconnect events from this task's log stream."""
    task_id = task_arn.rsplit("/", 1)[-1]
    events, token = [], None
    while True:
        kwargs = {
            "logGroupName": LOG_GROUP,
            "logStreamNamePrefix": LOG_STREAM_PREFIX + task_id,
            "filterPattern": '?"Player connected" ?"Player disconnected"',
            "limit": 200,
        }
        if token:
            kwargs["nextToken"] = token
        page = logs.filter_log_events(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            return events


def _players_online(task_arn):
    """(sorted names currently online, ms timestamp of last player event)."""
    online, last_ts = {}, 0
    for ev in _player_events(task_arn):
        m = _PLAYER_RE.search(ev.get("message", ""))
        if not m:
            continue
        last_ts = max(last_ts, ev.get("timestamp", 0))
        name = m.group(2).strip()
        online[name] = m.group(1) == "connected"
    names = sorted(n for n, on in online.items() if on)
    return names, last_ts


def _dns_sync(event):
    """Handle ECS Task State Change: point server.* at the task IP."""
    detail = event.get("detail") or {}
    task_arn = detail.get("taskArn", "")
    last = detail.get("lastStatus", "")
    if not HOSTED_ZONE_ID or not task_arn or not SERVER_FQDN:
        return {"action": "noop", "reason": "dns not configured"}

    tasks = ecs.describe_tasks(cluster=CLUSTER, tasks=[task_arn]).get("tasks", [])
    task = tasks[0] if tasks else None

    if last == "RUNNING" and task:
        ip = _public_ip(task)
        if not ip:
            return {"action": "noop", "reason": "no public ip yet"}
        route53.change_resource_record_sets(
            HostedZoneId=HOSTED_ZONE_ID,
            ChangeBatch={
                "Changes": [{
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": SERVER_FQDN, "Type": "A", "TTL": 60,
                        "ResourceRecords": [{"Value": ip}],
                    },
                }]
            },
        )
        return {"action": "upserted", "fqdn": SERVER_FQDN, "ip": ip}

    if last == "STOPPED":
        route53.change_resource_record_sets(
            HostedZoneId=HOSTED_ZONE_ID,
            ChangeBatch={
                "Changes": [{
                    "Action": "DELETE",
                    "ResourceRecordSet": {
                        "Name": SERVER_FQDN, "Type": "A", "TTL": 60,
                        "ResourceRecords": [{"Value": "127.0.0.1"}],
                    },
                }]
            },
        )
        return {"action": "deleted", "fqdn": SERVER_FQDN}

    return {"action": "noop", "reason": last}


def _serve_map_tile(path):
    """Proxy a map tile/static file from the S3 bucket."""
    key = path.lstrip("/") or "index.html"
    if ".." in key or key.startswith("/"):
        return _resp(400, {"error": "bad path"})
    try:
        obj = s3.get_object(Bucket=MAP_TILES_BUCKET, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404", "AccessDenied"):
            return _resp(404, {"error": "not found"})
        raise
    body = obj["Body"].read()
    ext = os.path.splitext(key)[1].lower()
    ctype = _TILE_TYPES.get(ext, "application/octet-stream")
    binary = ctype.startswith("image/") or ctype in ("application/octet-stream", "application/wasm", "image/x-icon")
    return {
        "statusCode": 200,
        "headers": {"Content-Type": ctype, "Cache-Control": "public, max-age=120"},
        "isBase64Encoded": binary,
        "body": base64.b64encode(body).decode() if binary else body.decode("utf-8", "replace"),
    }


def _status():
    svc = _service()
    desired = svc.get("desiredCount", 0)
    task = _current_task()

    ip = None
    started_at = task.get("startedAt") or task.get("createdAt") if task else None
    last_status = task.get("lastStatus") if task else None

    if task and last_status in ("PROVISIONING", "PENDING", "ACTIVATING", "RUNNING"):
        ip = _public_ip(task)

    reachable = _tcp_reachable(ip) if ip else False

    players, last_ev_ts = ([], 0)
    if task and last_status == "RUNNING":
        try:
            players, last_ev_ts = _players_online(task["taskArn"])
        except Exception:
            players, last_ev_ts = [], 0

    active = ("PROVISIONING", "PENDING", "ACTIVATING", "RUNNING",
              "DEACTIVATING", "STOPPING", "DEPROVISIONING")
    if desired == 0:
        state = "STOPPING" if task and last_status in active else "STOPPED"
    elif reachable:
        state = "ONLINE"
    else:
        state = "STARTING"

    uptime = None
    if started_at:
        uptime = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds() // 60))

    if MAP_FQDN:
        map_url = f"https://{MAP_FQDN}/"
    elif ip and MAP_ENABLED:
        map_url = f"http://{ip}/"
    else:
        map_url = None

    return {
        "state": state,
        "serverName": SERVER_NAME,
        "desiredCount": desired,
        "taskStatus": last_status,
        "address": SERVER_FQDN or ip,
        "ip": ip,
        "port": GAME_PORT,
        "mapUrl": map_url,
        "adminUrl": ADMIN_URL or None,
        "reachable": reachable,
        "players": players,
        "uptimeMinutes": uptime,
        "idleShutdownMinutes": IDLE_MINUTES,
        "checkedAt": int(time.time()),
    }


def _autostop_check():
    svc = _service()
    if svc.get("desiredCount", 0) != 1:
        return {"action": "noop", "reason": "server already stopping/stopped"}

    task = _current_task()
    if not task or task.get("lastStatus") != "RUNNING":
        return {"action": "noop", "reason": "no running task"}

    players, last_ts = _players_online(task["taskArn"])
    if players:
        return {"action": "noop", "reason": f"{len(players)} player(s) online"}

    # Idle clock starts at task start and resets on every connect/disconnect.
    started = task.get("startedAt") or task.get("createdAt")
    idle_since_ms = last_ts or (started.timestamp() * 1000 if started else 0)
    idle_mins = (time.time() * 1000 - idle_since_ms) / 60000

    if idle_mins < IDLE_MINUTES:
        return {"action": "noop", "reason": f"empty for {idle_mins:.0f}m (< {IDLE_MINUTES}m)"}

    ecs.update_service(cluster=CLUSTER, service=SERVICE, desiredCount=0)
    return {"action": "stopped", "idleMinutes": round(idle_mins)}


def lambda_handler(event, _ctx):
    # ECS Task State Change -> keep server.* DNS pointed at the task IP
    if event.get("detail-type") == "ECS Task State Change":
        return _dns_sync(event)

    # Scheduled auto-stop invocation
    if event.get("source") == "aws.events" or "detail-type" in event:
        return _autostop_check()

    http = (event.get("requestContext") or {}).get("http") or {}
    path = event.get("rawPath") or http.get("path") or "/"
    method = http.get("method", "GET")
    host = (event.get("headers") or {}).get("host", "")

    # map.<domain> -> proxy rendered tiles from S3
    if MAP_FQDN and host.split(":")[0] == MAP_FQDN:
        if method != "GET":
            return _resp(405, {"error": "method not allowed"})
        return _serve_map_tile(path)

    # admin.<domain> or /admin -> the approval SPA (Cognito login inside the page)
    is_admin_host = ADMIN_FQDN and host.split(":")[0] == ADMIN_FQDN
    if ACCESS_ENABLED and (is_admin_host or path in ("/admin", "/admin/")):
        if path.startswith("/api/"):
            pass  # fall through to admin API below
        elif method != "GET":
            return _resp(405, {"error": "method not allowed"})
        else:
            page = (_admin_html
                    .replace("__COGNITO_DOMAIN__", COGNITO_DOMAIN)
                    .replace("__COGNITO_CLIENT_ID__", COGNITO_CLIENT_ID)
                    .replace("__ADMIN_URL__", ADMIN_URL))
            return _resp(200, page, "text/html; charset=utf-8")

    if ACCESS_ENABLED and path.startswith("/api/admin/"):
        if not _admin_authed(event):
            return _resp(401, {"error": "admin login required"})
        if path == "/api/admin/requests" and method == "GET":
            data = _access_store()
            running = _service().get("desiredCount", 0) > 0
            return _resp(200, {"requests": data["requests"],
                               "players": _players_list(data),
                               "serverRunning": running})
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            body = {}
        if path == "/api/admin/approve" and method == "POST":
            return _admin_approve(body)
        if path == "/api/admin/reject" and method == "POST":
            return _admin_reject(body)
        return _resp(404, {"error": "not found"})

    if path in ("/", "/index.html"):
        return _resp(200, _html, "text/html; charset=utf-8")

    if not path.startswith("/api/"):
        return _resp(404, {"error": "not found"})

    if not _authed(event):
        return _resp(401, {"error": "bad or missing password"})

    if path == "/api/status" and method == "GET":
        return _resp(200, _status())

    if ACCESS_ENABLED and path == "/api/lookup-xuid" and method == "GET":
        gamertag = (event.get("queryStringParameters") or {}).get("gamertag", "")
        if not gamertag.strip():
            return _resp(400, {"error": "gamertag required"})
        try:
            found = _lookup_xuid(gamertag)
        except Exception:
            return _resp(502, {"error": "xuid lookup failed - try https://rghlab.co.uk/xuid/"})
        return _resp(200, {"found": bool(found), **(found or {})})

    if ACCESS_ENABLED and path == "/api/access-request" and method == "POST":
        try:
            gamertag = json.loads(event.get("body") or "{}").get("gamertag", "")
        except json.JSONDecodeError:
            gamertag = ""
        return _request_access(gamertag)

    if path == "/api/control" and method == "POST":
        try:
            action = json.loads(event.get("body") or "{}").get("action")
        except json.JSONDecodeError:
            action = None
        if action not in ("start", "stop"):
            return _resp(400, {"error": "action must be start or stop"})
        ecs.update_service(
            cluster=CLUSTER, service=SERVICE, desiredCount=1 if action == "start" else 0
        )
        return _resp(200, {"ok": True, "action": action})

    return _resp(404, {"error": "not found"})
