"""Ownership feature package.

* ``registry``  — owners + assignments JSON registries (soft-delete Trash).
* ``resolve``   — the effective-owner resolution engine (precedence: direct → tag →
                  workload → inherited ancestor scope → unowned).
* ``cache``     — server-side coverage-snapshot cache (6h TTL, read-cache-only-on-visit).
* ``directory`` — federated people-picker (SSO app users + live Entra search + manual).
* ``coverage``  — owner-coverage %, unowned/orphan detection, ownership-policy findings.
* ``suggest``   — AI/heuristic owner inference from tags/RBAC/created-by signals.
* ``demo``      — seed owners/teams/assignments over the demo workloads.
* ``agent_tool``— read-only chat tools (who_owns / what_does_owner_own / find_unowned).
"""
