"""Runtime registry of sandbox troubleshooting VMs (admin-managed, SSH-reachable).

A *sandbox VM* is a dedicated, disposable troubleshooting box the admin pre-provisions
and onboards here. It sits inside (or peered to) a workload's VNet so the agent can run
diagnostic commands FROM the box — reaching private endpoints the backend can't. The
LLM is told the box's OS + installed toolkit and given a general ``vm_exec`` tool; it
composes its own commands.

Mirrors :mod:`app.core.azure_connections`: a flat JSON registry under backend/.data,
secrets (SSH private key / passphrase / password) encrypted at rest via app.core.crypto.
Because these are sandboxes (low blast radius), the default posture is autonomous shell;
an optional per-VM ``strict_mode`` routes destructive commands through the approval gate.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.crypto import decrypt, encrypt

_PATH = Path(__file__).resolve().parents[2] / ".data" / "sandbox_vms.json"

AUTH_METHODS = ("ssh_key", "ssh_password")

# Fields that hold secrets and must be encrypted at rest.
_SECRET_FIELDS = ("ssh_private_key", "ssh_passphrase", "ssh_password")

_DEFAULTS: dict[str, Any] = {
    "display_name": "",
    "host": "",
    "port": 22,
    "username": "",
    "auth_method": "ssh_password",  # ssh_key | ssh_password
    # Governance.
    "strict_mode": False,  # True = destructive commands need approval; False = autonomous
    "disabled": False,
    "allow_sudo": True,  # may the agent use sudo on this box (e.g. to auto-install tools)?
    # Workloads this sandbox can serve (its VNet reach). Resolved in chat.
    "workload_ids": [],
    "vnet_label": "",  # human label of the network the box can reach (e.g. "prod-vnet")
    # Discovered environment (populated on Test / first use).
    "os_info": "",  # e.g. "Ubuntu 24.04.1 LTS (Linux 6.8.0 x86_64)"
    "capabilities": [],  # detected toolkit: ["dig", "curl", "nc", "mtr", "az", ...]
    "pkg_manager": "",  # apt-get | dnf | yum | apk | zypper | pacman (for installing tools)
    "can_sudo": False,  # sudo usable unattended (passwordless OR password-sudo via login pw)
    "sudo_mode": "none",  # none | passwordless | password (how the agent must invoke sudo)
    # Credentials (encrypted when persisted).
    "ssh_private_key": "",
    "ssh_passphrase": "",
    "ssh_password": "",
    # Host-key trust (TOFU): pinned on first successful Test; mismatch is rejected.
    "host_key_fingerprint": "",
    # Health.
    "status": "unknown",  # unknown | ok | error
    "status_detail": "",
    "last_tested": "",
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"vms": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge_defaults(vm: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(_DEFAULTS))  # deep copy (lists)
    merged.update(vm or {})
    return merged


def list_vms() -> list[dict[str, Any]]:
    """All sandbox VMs with secrets DECRYPTED (for internal/server use only)."""
    data = _read()
    out: list[dict[str, Any]] = []
    for vid, vm in data.get("vms", {}).items():
        merged = _merge_defaults(vm)
        merged["id"] = vid
        for f in _SECRET_FIELDS:
            merged[f] = decrypt(merged.get(f, ""))
        out.append(merged)
    out.sort(key=lambda v: (v.get("display_name", "").lower(), v.get("host", "")))
    return out


def get_vm(vm_id: str) -> dict[str, Any] | None:
    if not vm_id:
        return None
    for vm in list_vms():
        if vm["id"] == vm_id:
            return vm
    return None


def resolve_for_workload(workload_id: str) -> list[dict[str, Any]]:
    """Every enabled sandbox linked to a workload (its VNet reach), decrypted."""
    if not workload_id:
        return []
    return [
        vm for vm in list_vms()
        if not vm.get("disabled") and workload_id in (vm.get("workload_ids") or [])
    ]


def upsert_vm(vm: dict[str, Any]) -> dict[str, Any]:
    """Create or update a sandbox VM. Secrets encrypted before write; an empty secret
    field on update means 'keep the existing value'."""
    data = _read()
    vms = data.setdefault("vms", {})
    vid = vm.get("id") or str(uuid.uuid4())
    existing = vms.get(vid, {})

    merged = _merge_defaults(existing)
    for key in _DEFAULTS:
        if key in vm and vm[key] is not None:
            merged[key] = vm[key]

    # Encrypt secrets; blank on update keeps the stored (encrypted) value.
    for f in _SECRET_FIELDS:
        incoming = vm.get(f)
        if incoming:
            merged[f] = encrypt(incoming)
        else:
            merged[f] = existing.get(f, "")

    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    vms[vid] = merged

    _write(data)
    result = get_vm(vid)
    assert result is not None
    return result


def delete_vm(vm_id: str) -> bool:
    data = _read()
    vms = data.get("vms", {})
    if vm_id in vms:
        del vms[vm_id]
        _write(data)
        return True
    return False


def update_status(vm_id: str, status: str, detail: str = "", *, tested: bool = True) -> None:
    data = _read()
    vms = data.get("vms", {})
    if vm_id not in vms:
        return
    vms[vm_id]["status"] = status
    vms[vm_id]["status_detail"] = detail
    if tested:
        vms[vm_id]["last_tested"] = _now()
    _write(data)


def update_environment(
    vm_id: str, *, os_info: str = "", capabilities: list[str] | None = None,
    host_key_fingerprint: str | None = None, pkg_manager: str | None = None,
    can_sudo: bool | None = None, sudo_mode: str | None = None,
) -> None:
    """Persist detected OS/toolkit + (optionally) pin the host-key fingerprint."""
    data = _read()
    vms = data.get("vms", {})
    if vm_id not in vms:
        return
    if os_info:
        vms[vm_id]["os_info"] = os_info
    if capabilities is not None:
        vms[vm_id]["capabilities"] = capabilities
    if host_key_fingerprint is not None:
        vms[vm_id]["host_key_fingerprint"] = host_key_fingerprint
    if pkg_manager is not None:
        vms[vm_id]["pkg_manager"] = pkg_manager
    if can_sudo is not None:
        vms[vm_id]["can_sudo"] = bool(can_sudo)
    if sudo_mode is not None:
        vms[vm_id]["sudo_mode"] = sudo_mode
    vms[vm_id]["updated_at"] = _now()
    _write(data)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "••••"
    return value[:4] + "…" + value[-4:]


def public_vm(vm: dict[str, Any]) -> dict[str, Any]:
    """A single VM safe for the UI: secrets masked, never raw."""
    return {
        "id": vm["id"],
        "display_name": vm.get("display_name", ""),
        "host": vm.get("host", ""),
        "port": vm.get("port", 22),
        "username": vm.get("username", ""),
        "auth_method": vm.get("auth_method", "ssh_password"),
        "strict_mode": bool(vm.get("strict_mode", False)),
        "disabled": bool(vm.get("disabled", False)),
        "allow_sudo": bool(vm.get("allow_sudo", True)),
        "workload_ids": list(vm.get("workload_ids", []) or []),
        "vnet_label": vm.get("vnet_label", ""),
        "os_info": vm.get("os_info", ""),
        "capabilities": list(vm.get("capabilities", []) or []),
        "pkg_manager": vm.get("pkg_manager", ""),
        "can_sudo": bool(vm.get("can_sudo", False)),
        "sudo_mode": vm.get("sudo_mode", "none"),
        "has_private_key": bool(vm.get("ssh_private_key")),
        "has_password": bool(vm.get("ssh_password")),
        "host_key_fingerprint": vm.get("host_key_fingerprint", ""),
        "password_hint": _mask(vm.get("ssh_password", "")),
        "status": vm.get("status", "unknown"),
        "status_detail": vm.get("status_detail", ""),
        "last_tested": vm.get("last_tested", ""),
        "created_at": vm.get("created_at", ""),
        "updated_at": vm.get("updated_at", ""),
    }


def public_vms() -> list[dict[str, Any]]:
    return [public_vm(v) for v in list_vms()]
