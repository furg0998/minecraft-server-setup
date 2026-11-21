"""CloudFormation custom resource: merge the AllowedPlayers parameter with
admin-approved players in the player-access secret.

The secret's value is owned by this resource, not by the SecretString
property, so stack updates merge instead of overwrite:

  {"players": "<param>",           <- written from the parameter each deploy
    "approved": ["tag:xuid", ...], <- owned by the admin approval page
    "requests": [...],             <- pending access requests (preserved)
    "combined": "a:1,b:2"}         <- players + approved, injected into ECS
"""

import json
import urllib.request

import boto3
from botocore.exceptions import ClientError

secrets = boto3.client("secretsmanager")


def _merge(param_players, approved):
    seen, combined = set(), []
    for p in param_players.split(",") + approved:
        p = p.strip()
        if not p:
            continue
        key = p.rsplit(":", 1)[-1] if ":" in p else p.lower()
        if key not in seen:
            seen.add(key)
            combined.append(p)
    return ",".join(combined)


def _respond(event, status, reason=""):
    body = json.dumps({
        "Status": status,
        "Reason": reason or "see CloudWatch logs",
        "PhysicalResourceId": "allowlist-merge",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": {},
    }).encode()
    req = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"content-type": ""})
    urllib.request.urlopen(req, timeout=10).read()


def lambda_handler(event, _ctx):
    status, reason = "SUCCESS", ""
    try:
        if event["RequestType"] in ("Create", "Update"):
            props = event["ResourceProperties"]
            arn = props["SecretArn"]
            param_players = props.get("Players", "")
            try:
                raw = secrets.get_secret_value(SecretId=arn)["SecretString"]
                cur = json.loads(raw or "{}")
            except ClientError:
                cur = {}
            approved = cur.get("approved") or []
            secrets.put_secret_value(
                SecretId=arn,
                SecretString=json.dumps({
                    "players": param_players,
                    "approved": approved,
                    "requests": cur.get("requests") or [],
                    "combined": _merge(param_players, approved),
                }),
            )
        # Delete: leave the secret value alone (players stay on the list).
    except Exception as e:  # noqa: BLE001 - must always respond to CFN
        status, reason = "FAILED", f"{type(e).__name__}: {e}"[:1000]
    _respond(event, status, reason)
