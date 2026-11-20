"""Control panel + API for the on-demand Minecraft Bedrock server.

Routes (same API Gateway endpoint):
  GET  /            -> static single-page app
  GET  /api/status  -> server state, address, online players  (needs password)
  POST /api/control -> {"action": "start"|"stop"}             (needs password)

Also handles an EventBridge scheduled event for idle auto-stop.
"""

import hmac
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION") or os.environ["AWS_DEFAULT_REGION"]
CLUSTER = os.environ["CLUSTER_NAME"]
SERVICE = os.environ["SERVICE_NAME"]
LOG_GROUP = os.environ["LOG_GROUP_NAME"]
SECRET_ARN = os.environ["PANEL_SECRET_ARN"]
IDLE_MINUTES = int(os.environ.get("IDLE_SHUTDOWN_MINUTES", "45"))
GAME_PORT = int(os.environ.get("GAME_PORT", "19132"))
SERVER_NAME = os.environ.get("SERVER_NAME", "Minecraft Server")
LOG_STREAM_PREFIX = "minecraft/minecraft/"  # <awslogs-prefix>/<container>/<task-id>

ecs = boto3.client("ecs", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)

_password = None
_html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
_PLAYER_RE = re.compile(r"Player (connected|disconnected):\s*([^,]+)")


def _panel_password():
    global _password
    if _password is None:
        secret = secrets.get_secret_value(SecretId=SECRET_ARN)["SecretString"]
        _password = json.loads(secret)["password"]
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

    return {
        "state": state,
        "serverName": SERVER_NAME,
        "desiredCount": desired,
        "taskStatus": last_status,
        "address": ip,
        "port": GAME_PORT,
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
    # Scheduled auto-stop invocation
    if event.get("source") == "aws.events" or "detail-type" in event:
        return _autostop_check()

    http = (event.get("requestContext") or {}).get("http") or {}
    path = event.get("rawPath") or http.get("path") or "/"
    method = http.get("method", "GET")

    if path in ("/", "/index.html"):
        return _resp(200, _html, "text/html; charset=utf-8")

    if not path.startswith("/api/"):
        return _resp(404, {"error": "not found"})

    if not _authed(event):
        return _resp(401, {"error": "bad or missing password"})

    if path == "/api/status" and method == "GET":
        return _resp(200, _status())

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
