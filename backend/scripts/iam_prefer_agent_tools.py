"""Point the built-in Identity & Access agent at the IAM tools.

The plan's rule for this integration is "consume, do not duplicate": the agent currently
re-derives RBAC from raw Resource Graph queries on every run, which is slower, costs the
customer's ARG quota, and can *disagree with what the UI shows* because it re-implements
inheritance, notActions and deny evaluation in a prompt rather than calling the engine that
already does it.

The tools are inserted ABOVE the Resource Graph guidance rather than replacing it: ARG is still
the right answer for a scope that has never been scanned, and the prompt has to say which is
which. It also has to say what the tools refuse to answer — a model told only "use the tool"
will treat an UNMEASURED reply as a negative result.

Tool names are LLM-visible: this file and `agent_tool.build_iam_tools` must be changed together.
"""
import json
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "app" / "automations" / "builtin_agents.json"
KEY = "builtin-identity-access-agent"

ANCHOR = "# How to use tools\n\n- **Inventory first with Azure Resource Graph**"

INSERT = """# How to use tools

- **Prefer the cached access tools over re-querying Azure.** This product already holds a
  normalised access snapshot, and the tools below answer from it — instantly, without consuming
  Resource Graph quota, and consistent with what the user is looking at on the `/iam` screens.
  Re-deriving RBAC from raw ARG queries risks giving an answer that contradicts the UI.
  - `can_principal_do` — can this principal perform this action on this scope, and why. Honours
    deny assignments, notActions, control-plane vs data-plane and scope inheritance.
  - `why_does_principal_have_access` — where the access came from and **which assignment id to
    change**. Use this whenever the question is about removing access.
  - `who_can_reach_resource` — everyone who can reach one resource, including inherited access,
    plus whether it can be reached without any role assignment at all.
  - `escalation_paths_to` — paths to a more powerful role.
  - `unused_permissions_for` — granted versus actually used.
  - `simulate_revoke` — model a removal before recommending it. Changes nothing.
  - `access_changed_since` — what changed since the previous scan.
- **These tools tell you when they do not know, and you must pass that on.** A reply beginning
  `UNKNOWN` or `UNMEASURED`, or containing "not an all-clear", is *not* a negative result: it
  means the data was never collected. Never summarise it as "no access", "nothing unused" or
  "no escalation paths" — say what was not measured and what the user should run.
- **Fall back to Resource Graph when the tools have no snapshot** (an unscanned tenant or a
  scope outside the scan), and say which source you used.
- **Inventory first with Azure Resource Graph**"""

src = P.read_text(encoding="utf-8")
data = json.loads(src)
agent = data["agents"][KEY]
ins = agent["instructions"]

if "Prefer the cached access tools" in ins:
    print("already updated — no change")
    raise SystemExit(0)

assert ins.count(ANCHOR) == 1, f"anchor found {ins.count(ANCHOR)} times"
agent["instructions"] = ins.replace(ANCHOR, INSERT, 1)

P.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("identity agent prompt now prefers the IAM tools")
