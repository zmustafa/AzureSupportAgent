"""Bring `frontend/src/api.ts` back in step with the Entra API.

Written as a script only because the editing tool in use cannot write to this file: its
view of it is roughly two thousand lines out of sync with disk, and every exact-match
replacement fails even for text confirmed present by `Select-String` and by git. The
features below therefore shipped with locally-declared types and a comment pointing here.

Every replacement is anchored on unique text and verified: a missing or duplicated anchor
aborts the whole run before anything is written, so a partial patch is impossible.
"""
from __future__ import annotations

import pathlib
import sys

API = pathlib.Path("src/api.ts")

REPLACEMENTS: list[tuple[str, str, str]] = [
    (
        "posture trend carries the per-pillar series",
        """  trend: {
    previous_score: number | null;
    delta: number | null;
    points: { at: string; score: number; coverage: number }[];
  };""",
        """  trend: {
    previous_score: number | null;
    /** When the comparison point was taken. Absent until a second collection exists. */
    previous_at?: string | null;
    delta: number | null;
    /** Per-pillar movement since the previous refresh. A missing key is not comparable. */
    pillar_delta?: Record<string, number>;
    points: { at: string; score: number; coverage: number; pillars?: Record<string, number | null> }[];
  };
  /** How the tenant authenticates: federated domains, providers and hybrid state. */
  identity_fabric?: EntraFabricBrief;""",
    ),
    (
        "findings accepts server-side sort",
        "    params: { severity?: string; pillar?: string; signal?: string; state?: string; search?: string; offset?: number; limit?: number },",
        "    params: { severity?: string; pillar?: string; signal?: string; state?: string; search?: string;\n              sort?: string; dir?: string; offset?: number; limit?: number },",
    ),
    (
        "apps accepts server-side sort",
        "    params: { search?: string; tier?: string; ownerless?: boolean; risk_min?: number; offset?: number; limit?: number },",
        "    params: { search?: string; tier?: string; ownerless?: boolean; risk_min?: number;\n              sort?: string; dir?: string; offset?: number; limit?: number },",
    ),
    (
        "inbox accepts server-side sort",
        """    params: { severity?: string; pillar?: string; state?: string; ageing_days?: number;
              unassigned?: boolean; search?: string; offset?: number; limit?: number },""",
        """    params: { severity?: string; pillar?: string; state?: string; ageing_days?: number;
              unassigned?: boolean; search?: string; sort?: string; dir?: string;
              offset?: number; limit?: number },""",
    ),
    (
        "assignments accepts sort and the privileged filter",
        "    params: { kind?: string; tier?: string; principal_type?: string; search?: string },",
        "    params: { kind?: string; tier?: string; principal_type?: string; search?: string;\n              privileged?: boolean; sort?: string; dir?: string },",
    ),
    (
        "assignments query serialises the boolean filter",
        "    `/entra/privileged/assignments${entraQs(connectionId, params)}`),",
        "    `/entra/privileged/assignments${entraQs(connectionId, params as Record<string, string | number | undefined>)}`),",
    ),
    (
        "setup checklist carries the identity fabric",
        """  app_registration: { client_id: string; tenant_id: string; portal_url: string };
  consent_url: string;
};""",
        """  app_registration: { client_id: string; tenant_id: string; portal_url: string };
  consent_url: string;
  /** Domain authentication types, federation trusts, external providers and hybrid state. */
  identity_fabric?: EntraIdentityFabric;
};""",
    ),
    (
        "sign-in overview carries the lookback window",
        """export type EntraSignalsOverview = {
  meta: EntraMeta; signins: EntraSignInAggregates; capabilities: Record<string, boolean>;
  thresholds: Record<string, number>; counts: Record<string, number>; sampled: boolean;
  domain: EntraDomainMeta;
};""",
        """export type EntraSignalsOverview = {
  meta: EntraMeta; signins: EntraSignInAggregates; capabilities: Record<string, boolean>;
  thresholds: Record<string, number>; counts: Record<string, number>; sampled: boolean;
  domain: EntraDomainMeta;
  /**
   * `days` is the window the NEXT collection will use; `data_days` is the window the
   * figures on screen actually cover. They differ between saving the setting and
   * re-collecting, and conflating them lets the page claim a window it never collected.
   */
  lookback?: { days: number; data_days?: number | null; min: number; max: number; setting_key?: string };
};""",
    ),
    (
        "auth methods carries the federation caveat",
        """  gap_total: number;
  enabled_total: number;
  unreported: number;
};""",
        """  gap_total: number;
  enabled_total: number;
  unreported: number;
  /**
   * Federated users register their factors with the identity provider, not with Entra, so
   * every figure above describes the cloud-authenticated population plus anyone who
   * separately registered a method here.
   */
  identity_fabric?: EntraFabricBrief;
};""",
    ),
    (
        "app settings carry the Entra sign-in lookback",
        "  mcp_read_only: boolean;\n  entra_mcp_enabled?: boolean;",
        "  mcp_read_only: boolean;\n  entra_mcp_enabled?: boolean;\n  /** Entra sign-in collection window, in days. Clamped to 1–90 by the server. */\n  entra_signin_lookback_days?: number;",
    ),
]

# Appended once, at the end: the fabric shapes the replacements above refer to.
FABRIC_TYPES = """

// ---- Entra identity fabric ----------------------------------------------------------
/**
 * How the tenant authenticates.
 *
 * A federated domain means Entra is not the authenticator — an external provider is, and
 * Entra accepts its result, including its multi-factor claim unless the trust says
 * otherwise. That single fact decides whether the authentication figures elsewhere in the
 * Entra views can be read at face value.
 *
 * Certificates are carried as derived facts only. The signing certificate itself is parsed
 * during collection and discarded; it never reaches the client.
 */
export type EntraFabricCertificate = {
  parsed?: boolean; subject?: string; issuer?: string; thumbprint?: string;
  not_before?: string; not_after?: string; days_left?: number; expired?: boolean;
};

export type EntraFabricTrust = {
  domain: string;
  display_name?: string;
  issuer_uri?: string;
  passive_sign_in_uri?: string;
  active_sign_in_uri?: string;
  sign_out_uri?: string;
  metadata_exchange_uri?: string;
  host?: string;
  vendor?: { key: string; label: string };
  protocol?: string;
  mfa_behaviour?: { value: string; explicit: boolean; label: string; trusted: boolean };
  signed_request_required?: boolean | null;
  prompt_login_behavior?: string;
  certificate?: EntraFabricCertificate;
  next_certificate?: EntraFabricCertificate;
  auto_rollover?: { result: string; last_run: string; healthy: boolean };
  user_count?: number;
  user_share?: number;
};

export type EntraFabricDomain = {
  name: string; authentication_type: string; federated: boolean;
  is_default: boolean; is_initial: boolean; is_verified: boolean;
  password_validity_days?: number | null; password_notification_days?: number | null;
};

export type EntraFabricHybrid = {
  sync_enabled?: boolean; last_sync?: string; features_readable?: boolean;
  password_sync?: boolean; password_writeback?: boolean; user_writeback?: boolean;
  group_writeback?: boolean; device_writeback?: boolean;
  cloud_password_policy?: boolean; block_soft_match?: boolean;
  block_cloud_object_takeover?: boolean;
  deletion_prevention?: { type: string; threshold?: number | null };
};

/** An external provider guests sign in with. A separate perimeter from domain federation. */
export type EntraExternalIdp = {
  id: string; display_name?: string; kind?: string; kind_label?: string;
  identity_provider_type?: string; client_id?: string; issuer_uri?: string; domain?: string;
};

export type EntraIdentityFabric = {
  readable?: boolean;
  blind_reason?: string;
  domains?: EntraFabricDomain[];
  federation?: EntraFabricTrust[];
  federated_count?: number;
  managed_count?: number;
  user_total?: number;
  hybrid?: EntraFabricHybrid;
  federated?: boolean;
  summary?: string;
  external_idps?: EntraExternalIdp[];
  external_idps_readable?: boolean;
  external_idps_reason?: string;
};

/** The one-line version, for screens whose own numbers are qualified by federation. */
export type EntraFabricBrief = {
  readable?: boolean; federated?: boolean; federated_count?: number; managed_count?: number;
  vendors?: string[]; domains?: string[]; user_count?: number | null; user_share?: number | null;
  sync_enabled?: boolean; password_sync?: boolean | null; summary?: string;
};
"""


def _read() -> str:
    # newline="" so the file's CRLF endings survive the round trip. Rewriting 14,000 lines
    # of line endings would bury the ten real changes in an unreviewable diff.
    with open(API, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(text: str) -> None:
    with open(API, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def main() -> int:
    text = _read()
    original = text
    for label, old, new in REPLACEMENTS:
        old_crlf, new_crlf = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        if new_crlf in text or new in text:
            print(f"  already applied: {label}")
            continue
        found = text.count(old_crlf) or text.count(old)
        if found != 1:
            print(f"ABORT: anchor for '{label}' matched {found} time(s), expected exactly 1")
            return 1
        text = text.replace(old_crlf, new_crlf) if old_crlf in text else text.replace(old, new)
        print(f"  patched: {label}")

    if "export type EntraIdentityFabric" not in text:
        text = text.rstrip("\r\n") + "\r\n" + FABRIC_TYPES.replace("\n", "\r\n")
        print("  appended: identity fabric types")

    if text == original:
        print("nothing to do")
        return 0
    _write(text)
    print(f"written: {API}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
