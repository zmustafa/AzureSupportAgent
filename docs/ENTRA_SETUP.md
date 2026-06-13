# EntraID (Microsoft Graph) MCP Server

In addition to the Azure MCP server, the app integrates the **EntraID MCP Server**. It
exposes Microsoft Graph tools for Entra ID (Azure AD): users, groups, app registrations
& service principals, **secret/certificate expiry**, MFA status, sign-in & audit logs,
and conditional-access policies. It is spawned over stdio just like the Azure MCP server
and its tools flow into the same provider tool-calling loop (works with every LLM
provider, including the Copilot/Codex guided-tool-calling adapters).

- **Enable for the default assistant:** Settings → **EntraID MCP Tools** → toggle on.
- **Per sub agent:** check *"Also allow all EntraID (Microsoft Graph) tools (MCP)"*
  in the agent editor (next to the Azure tools checkbox).
- **Identity:** it authenticates to Graph using the **default Azure connection's**
  service-principal credentials (tenant id / client id / client secret, or certificate).
- **Tools listing (admin):** `http://localhost:8000/api/admin/entra/tools`

## Required Microsoft Graph permissions

Grant these **Application** permissions to the app registration used by the connection,
then grant admin consent:

| API / Permission | Type | Description |
| --- | --- | --- |
| `AuditLog.Read.All` | Application | Read all audit log data |
| `AuthenticationContext.Read.All` | Application | Read all authentication context information |
| `DeviceManagementManagedDevices.Read.All` | Application | Read Microsoft Intune devices |
| `Directory.Read.All` | Application | Read directory data |
| `Group.Read.All` | Application | Read all groups |
| `GroupMember.Read.All` | Application | Read all group memberships |
| `Group.ReadWrite.All` | Application | Create, update, delete groups; manage group members and owners |
| `Policy.Read.All` | Application | Read your organization's policies |
| `RoleManagement.Read.Directory` | Application | Read all directory RBAC settings |
| `User.Read.All` | Application | Read all users' full profiles |
| `User-PasswordProfile.ReadWrite.All` | Application | Least privileged permission to update the passwordProfile property |
| `UserAuthenticationMethod.Read.All` | Application | Read all users' authentication methods |
| `Application.ReadWrite.All` | Application | Create, update, and delete applications (app registrations) and service principals |

Read-only permissions are sufficient for most queries; the `*.ReadWrite.All` permissions
enable group, password, and application management. Write tools (create/update/delete/
reset) are gated behind the app's approval policy.

## Local setup notes

The Graph SDK has very deep file paths, so its dependencies live in a dedicated venv to
avoid the Windows 260-char path limit (Windows long-path support is also enabled). The
backend spawns the server using `ENTRA_MCP_COMMAND` (that venv's python) and
`ENTRA_MCP_ARGS` (the stdio launcher `third_party/entraid-mcp-server/run_server.py`).
Override these via environment variables if your paths differ.

## Auth (local dev)

`DEV_AUTH=true` injects a fake admin identity so you can use the chat and admin
dashboard without standing up an external IdP. Set `DEV_AUTH_ROLE=user` to test the
non-admin view. Real OIDC / SAML SSO is configured under Settings → Access Control.
