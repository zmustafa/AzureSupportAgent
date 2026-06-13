"""Shared AWS helpers for the AWS connectors (SQS, S3, Security Hub).

Two auth modes are supported via a common field set:
- ``keys``: static access key id + secret (+ optional session token).
- ``role``: an assumed IAM role (STS AssumeRole) on top of the host's default
            credential chain (env vars / shared config / EC2/ECS instance profile).

boto3 is synchronous, so callers run client calls via ``run_aws`` (asyncio.to_thread).
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from app.connectors.base import FieldSpec

# Reusable config fields shared by every AWS connector. Service-specific fields
# (queue URL, bucket, …) are appended per connector.
AWS_AUTH_FIELDS_KEYS: list[FieldSpec] = [
    FieldSpec(key="region", label="AWS region", placeholder="us-east-1"),
    FieldSpec(key="access_key_id", label="Access key ID", type="password", secret=True),
    FieldSpec(key="secret_access_key", label="Secret access key", type="password", secret=True),
    FieldSpec(
        key="session_token",
        label="Session token",
        type="password",
        secret=True,
        optional=True,
        help="Only for temporary STS credentials.",
    ),
]

AWS_AUTH_FIELDS_ROLE: list[FieldSpec] = [
    FieldSpec(key="region", label="AWS region", placeholder="us-east-1"),
    FieldSpec(
        key="role_arn",
        label="Role ARN to assume",
        placeholder="arn:aws:iam::123456789012:role/azsupagent",
    ),
    FieldSpec(key="external_id", label="External ID", optional=True),
    FieldSpec(
        key="access_key_id",
        label="Base access key ID",
        type="password",
        secret=True,
        optional=True,
        help="Optional — the credentials used to call AssumeRole. Leave blank to use the host's instance profile / environment.",
    ),
    FieldSpec(key="secret_access_key", label="Base secret access key", type="password", secret=True, optional=True),
]


def _session(config: dict[str, Any]):
    """Build a boto3 Session honoring the connector's auth mode."""
    import boto3  # local import so the app boots without boto3 if unused

    region = config.get("region") or None
    mode = config.get("mode", "keys")
    base_kwargs: dict[str, Any] = {"region_name": region}
    if config.get("access_key_id"):
        base_kwargs["aws_access_key_id"] = config["access_key_id"]
    if config.get("secret_access_key"):
        base_kwargs["aws_secret_access_key"] = config["secret_access_key"]
    if config.get("session_token"):
        base_kwargs["aws_session_token"] = config["session_token"]
    session = boto3.Session(**base_kwargs)

    if mode == "role" and config.get("role_arn"):
        sts = session.client("sts")
        assume_kwargs: dict[str, Any] = {
            "RoleArn": config["role_arn"],
            "RoleSessionName": "azsupagent",
        }
        if config.get("external_id"):
            assume_kwargs["ExternalId"] = config["external_id"]
        creds = sts.assume_role(**assume_kwargs)["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return session


def aws_client(config: dict[str, Any], service: str):
    """Return a boto3 client for ``service`` using the connector's credentials."""
    return _session(config).client(service)


async def run_aws(fn: Callable[[], Any]) -> Any:
    """Run a synchronous boto3 call off the event loop."""
    return await asyncio.to_thread(fn)


def aws_config_valid(config: dict[str, Any]) -> str | None:
    """Return an error string if required AWS fields are missing, else None."""
    if not config.get("region"):
        return "AWS region is required."
    mode = config.get("mode", "keys")
    if mode == "keys" and not (config.get("access_key_id") and config.get("secret_access_key")):
        return "Access key ID and secret access key are required."
    if mode == "role" and not config.get("role_arn"):
        return "Role ARN is required for role-based auth."
    return None
