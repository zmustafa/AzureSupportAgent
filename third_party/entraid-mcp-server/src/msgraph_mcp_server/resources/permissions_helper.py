import logging
from typing import Dict, List, Any, Optional
from kiota_abstractions.base_request_configuration import RequestConfiguration
from utils.graph_client import GraphClient

logger = logging.getLogger(__name__)

# Microsoft Graph application ID - this is the constant ID for the Microsoft Graph service principal
MS_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"

# Common permission mappings - mapping of common tasks to their required permissions
# Format: {
#   "task_category": {
#     "task_name": {
#       "delegated": ["Permission1", "Permission2"],
#       "application": ["Permission1", "Permission2"],
#       "description": "Description of the task"
#     }
#   }
# }
COMMON_PERMISSION_MAPPINGS = {
    "users": {
        "read_user_profile": {
            "delegated": ["User.Read", "User.ReadBasic.All"],
            "application": ["User.Read.All"],
            "description": "Read user profile information"
        },
        "update_user_profile": {
            "delegated": ["User.ReadWrite", "User.ReadWrite.All"],
            "application": ["User.ReadWrite.All"],
            "description": "Update user profile information"
        },
        "read_all_users": {
            "delegated": ["User.ReadBasic.All", "User.Read.All"],
            "application": ["User.Read.All"],
            "description": "Read all users' profiles in the organization"
        },
        "reset_user_password": {
            "delegated": ["User.ReadWrite.All"],
            "application": ["User.ReadWrite.All", "Directory.ReadWrite.All"],
            "description": "Reset a user's password"
        }
    },
    "groups": {
        "read_user_groups": {
            "delegated": ["GroupMember.Read.All"],
            "application": ["GroupMember.Read.All", "Directory.Read.All"],
            "description": "Read groups a user is a member of"
        },
        "read_all_groups": {
            "delegated": ["Group.Read.All"],
            "application": ["Group.Read.All"],
            "description": "Read all groups in the organization"
        },
        "manage_groups": {
            "delegated": ["Group.ReadWrite.All"],
            "application": ["Group.ReadWrite.All"],
            "description": "Create, update, and delete groups, and add/remove members"
        }
    },
    "mail": {
        "read_user_mail": {
            "delegated": ["Mail.Read"],
            "application": ["Mail.Read"],
            "description": "Read user's mail"
        },
        "send_mail": {
            "delegated": ["Mail.Send"],
            "application": ["Mail.Send"],
            "description": "Send mail as the user"
        }
    },
    "calendar": {
        "read_user_calendar": {
            "delegated": ["Calendars.Read"],
            "application": ["Calendars.Read"],
            "description": "Read user's calendar"
        },
        "edit_user_calendar": {
            "delegated": ["Calendars.ReadWrite"],
            "application": ["Calendars.ReadWrite"],
            "description": "Read and write to user's calendar"
        }
    },
    "files": {
        "read_user_files": {
            "delegated": ["Files.Read", "Files.Read.All"],
            "application": ["Files.Read.All"],
            "description": "Read user's files"
        },
        "edit_user_files": {
            "delegated": ["Files.ReadWrite", "Files.ReadWrite.All"],
            "application": ["Files.ReadWrite.All"],
            "description": "Read and write to user's files"
        }
    },
    "devices": {
        "read_devices": {
            "delegated": ["Device.Read"],
            "application": ["Device.Read.All"],
            "description": "Read device information"
        },
        "manage_devices": {
            "delegated": ["Device.ReadWrite.All"],
            "application": ["Device.ReadWrite.All"],
            "description": "Manage device configuration"
        }
    },
    "audit_logs": {
        "read_audit_logs": {
            "delegated": ["AuditLog.Read.All"],
            "application": ["AuditLog.Read.All"],
            "description": "Read audit logs"
        },
        "read_sign_in_logs": {
            "delegated": ["AuditLog.Read.All"],
            "application": ["AuditLog.Read.All"],
            "description": "Read sign-in activity logs"
        }
    },
    "directory": {
        "read_directory": {
            "delegated": ["Directory.Read.All"],
            "application": ["Directory.Read.All"],
            "description": "Read directory data (users, groups, apps, etc.)"
        },
        "write_directory": {
            "delegated": ["Directory.ReadWrite.All"],
            "application": ["Directory.ReadWrite.All"],
            "description": "Read and write directory data (users, groups, apps, etc.)"
        }
    }
}

async def suggest_permissions_for_task(task_category: str, task_name: str) -> Dict[str, Any]:
    """Suggest permissions for a specific task based on common mappings.
    
    Args:
        task_category: The category of the task (users, groups, mail, etc.)
        task_name: The specific task name
        
    Returns:
        A dictionary with suggested delegated and application permissions
    """
    try:
        if task_category not in COMMON_PERMISSION_MAPPINGS:
            return {
                "status": "error",
                "message": f"Unknown task category: {task_category}",
                "available_categories": list(COMMON_PERMISSION_MAPPINGS.keys())
            }
            
        if task_name not in COMMON_PERMISSION_MAPPINGS[task_category]:
            return {
                "status": "error",
                "message": f"Unknown task name: {task_name}",
                "available_tasks": list(COMMON_PERMISSION_MAPPINGS[task_category].keys())
            }
            
        task_info = COMMON_PERMISSION_MAPPINGS[task_category][task_name]
        
        return {
            "status": "success",
            "task_category": task_category,
            "task_name": task_name,
            "description": task_info["description"],
            "delegated_permissions": task_info["delegated"],
            "application_permissions": task_info["application"],
            "notes": "These are suggested permissions based on common usage patterns. Always follow the principle of least privilege."
        }
    except Exception as e:
        logger.error(f"Error suggesting permissions for task {task_category}/{task_name}: {str(e)}")
        raise

async def list_available_categories_and_tasks() -> Dict[str, Any]:
    """List all available categories and tasks for permission suggestions.
    
    Returns:
        A dictionary with all available categories and their tasks
    """
    try:
        result = {
            "status": "success",
            "categories": {}
        }
        
        for category, tasks in COMMON_PERMISSION_MAPPINGS.items():
            result["categories"][category] = {
                "tasks": []
            }
            
            for task_name, task_info in tasks.items():
                result["categories"][category]["tasks"].append({
                    "name": task_name,
                    "description": task_info["description"]
                })
                
        return result
    except Exception as e:
        logger.error(f"Error listing available categories and tasks: {str(e)}")
        raise

async def get_all_graph_permissions(graph_client: GraphClient) -> Dict[str, Any]:
    """Get all Microsoft Graph permissions directly from the Microsoft Graph API.
    
    Args:
        graph_client: GraphClient instance
        
    Returns:
        A dictionary with all delegated and application permissions
    """
    try:
        client = graph_client.get_client()

        # MS_GRAPH_APP_ID is the well-known APP ID (client id), not the service principal
        # OBJECT id. by_service_principal_id() expects the object id, so look the SP up by
        # its appId filter first (passing the appId straight to by_service_principal_id
        # returns 404 Request_ResourceNotFound).
        from msgraph.generated.service_principals.service_principals_request_builder import (
            ServicePrincipalsRequestBuilder,
        )
        query_params = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetQueryParameters(
            filter=f"appId eq '{MS_GRAPH_APP_ID}'",
            select=["id", "appId", "displayName", "appRoles", "oauth2PermissionScopes"],
        )
        request_configuration = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetRequestConfiguration(
            query_parameters=query_params
        )
        sp_response = await client.service_principals.get(request_configuration=request_configuration)
        ms_graph_sp = sp_response.value[0] if (sp_response and sp_response.value) else None

        if not ms_graph_sp:
            logger.error("Microsoft Graph service principal not found")
            return {"status": "error", "message": "Microsoft Graph service principal not found"}
        
        # Extract delegated permissions (oauth2PermissionScopes)
        delegated_permissions = []
        if hasattr(ms_graph_sp, "oauth2_permission_scopes") and ms_graph_sp.oauth2_permission_scopes:
            for permission in ms_graph_sp.oauth2_permission_scopes:
                delegated_permissions.append({
                    "id": getattr(permission, "id", None),
                    "value": getattr(permission, "value", None),
                    "type": "delegated",
                    "adminConsentDisplayName": getattr(permission, "admin_consent_display_name", None),
                    "adminConsentDescription": getattr(permission, "admin_consent_description", None),
                    "userConsentDisplayName": getattr(permission, "user_consent_display_name", None),
                    "userConsentDescription": getattr(permission, "user_consent_description", None),
                    "isEnabled": getattr(permission, "is_enabled", None)
                })
        
        # Extract application permissions (appRoles)
        application_permissions = []
        if hasattr(ms_graph_sp, "app_roles") and ms_graph_sp.app_roles:
            for permission in ms_graph_sp.app_roles:
                application_permissions.append({
                    "id": getattr(permission, "id", None),
                    "value": getattr(permission, "value", None),
                    "type": "application",
                    "displayName": getattr(permission, "display_name", None),
                    "description": getattr(permission, "description", None),
                    "isEnabled": getattr(permission, "is_enabled", None)
                })
        
        return {
            "status": "success",
            "delegated_permissions": delegated_permissions,
            "application_permissions": application_permissions
        }
    except Exception as e:
        logger.error(f"Error getting Graph permissions: {str(e)}")
        raise

async def search_permissions(graph_client: GraphClient, search_term: str, permission_type: Optional[str] = None) -> Dict[str, Any]:
    """Search for Microsoft Graph permissions by keyword.
    
    Args:
        graph_client: GraphClient instance
        search_term: The keyword to search for
        permission_type: Optional filter by permission type ("delegated" or "application")
        
    Returns:
        A dictionary with matching permissions
    """
    try:
        all_permissions = await get_all_graph_permissions(graph_client)
        
        if all_permissions.get("status") != "success":
            return all_permissions
        
        delegated_permissions = all_permissions.get("delegated_permissions", [])
        application_permissions = all_permissions.get("application_permissions", [])
        
        # Convert search term to lowercase for case-insensitive matching
        search_term = search_term.lower()
        
        # Filter permissions based on search term
        matching_delegated = []
        if permission_type is None or permission_type.lower() == "delegated":
            for permission in delegated_permissions:
                # Search in value, display name, and description
                if (search_term in permission.get("value", "").lower() or
                    search_term in permission.get("adminConsentDisplayName", "").lower() or
                    search_term in permission.get("adminConsentDescription", "").lower()):
                    matching_delegated.append(permission)
        
        matching_application = []
        if permission_type is None or permission_type.lower() == "application":
            for permission in application_permissions:
                # Search in value, display name, and description
                if (search_term in permission.get("value", "").lower() or
                    search_term in permission.get("displayName", "").lower() or
                    search_term in permission.get("description", "").lower()):
                    matching_application.append(permission)
        
        return {
            "status": "success",
            "search_term": search_term,
            "matching_delegated_permissions": matching_delegated,
            "matching_application_permissions": matching_application,
            "total_matches": len(matching_delegated) + len(matching_application)
        }
    except Exception as e:
        logger.error(f"Error searching for permissions with term '{search_term}': {str(e)}")
        raise


# Application (app-role) Graph permissions the EntraID MCP server needs to function.
REQUIRED_GRAPH_PERMISSIONS: List[str] = [
    "AuditLog.Read.All",
    "AuthenticationContext.Read.All",
    "DeviceManagementManagedDevices.Read.All",
    "Directory.Read.All",
    "Group.Read.All",
    "GroupMember.Read.All",
    "Group.ReadWrite.All",
    "Policy.Read.All",
    "RoleManagement.Read.Directory",
    "User.Read.All",
    "User-PasswordProfile.ReadWrite.All",
    "UserAuthenticationMethod.Read.All",
    "Application.ReadWrite.All",
]


async def _sp_by_app_id(client, app_id: str, select: Optional[List[str]] = None):
    """Resolve a service principal by its appId (client id)."""
    from msgraph.generated.service_principals.service_principals_request_builder import (
        ServicePrincipalsRequestBuilder,
    )
    qp = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetQueryParameters(
        filter=f"appId eq '{app_id}'",
        select=select,
    )
    cfg = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetRequestConfiguration(
        query_parameters=qp
    )
    resp = await client.service_principals.get(request_configuration=cfg)
    return resp.value[0] if (resp and resp.value) else None


async def validate_app_permissions(graph_client: GraphClient, client_id: str) -> Dict[str, Any]:
    """Validate that the app identified by ``client_id`` (the connection's service
    principal) has the Microsoft Graph **application** permissions the EntraID MCP server
    needs, by reading its actual granted app-role assignments to the Microsoft Graph SP.

    Returns a structured report: which required permissions are granted, which are
    missing, any extra granted permissions, and whether the set is sufficient.
    """
    try:
        client = graph_client.get_client()

        # 1) The Microsoft Graph service principal — its appRoles map appRoleId -> value.
        graph_sp = await _sp_by_app_id(client, MS_GRAPH_APP_ID, select=["id", "appRoles"])
        if not graph_sp:
            return {"status": "error", "message": "Microsoft Graph service principal not found in tenant."}
        graph_sp_id = getattr(graph_sp, "id", None)
        role_id_to_value: Dict[str, str] = {}
        for role in (getattr(graph_sp, "app_roles", None) or []):
            rid = getattr(role, "id", None)
            val = getattr(role, "value", None)
            if rid and val:
                role_id_to_value[str(rid)] = val

        # 2) The app's own service principal (by appId == client_id).
        app_sp = await _sp_by_app_id(client, client_id, select=["id", "appId", "displayName"])
        if not app_sp:
            return {
                "status": "error",
                "message": (
                    f"No service principal found for appId '{client_id}'. Ensure the app "
                    "registration has an enterprise application (service principal) in this tenant."
                ),
            }
        app_sp_id = getattr(app_sp, "id", None)

        # 3) The app's granted application permissions (appRoleAssignments it holds).
        granted: List[str] = []
        unknown_role_ids: List[str] = []
        response = await client.service_principals.by_service_principal_id(app_sp_id).app_role_assignments.get()
        while response:
            for assignment in (getattr(response, "value", None) or []):
                # Only count assignments against the Microsoft Graph resource SP.
                if str(getattr(assignment, "resource_id", "")) != str(graph_sp_id):
                    continue
                rid = str(getattr(assignment, "app_role_id", "") or "")
                val = role_id_to_value.get(rid)
                if val:
                    granted.append(val)
                elif rid:
                    unknown_role_ids.append(rid)
            next_link = getattr(response, "odata_next_link", None)
            if next_link:
                response = await client.service_principals.by_service_principal_id(app_sp_id).app_role_assignments.with_url(next_link).get()
            else:
                break

        granted_set = set(granted)
        required = REQUIRED_GRAPH_PERMISSIONS
        missing = [p for p in required if p not in granted_set]
        present = [p for p in required if p in granted_set]
        extra = sorted(granted_set - set(required))

        return {
            "status": "success",
            "appId": getattr(app_sp, "app_id", None),
            "displayName": getattr(app_sp, "display_name", None),
            "servicePrincipalId": app_sp_id,
            "required": required,
            "granted": present,
            "missing": missing,
            "extra": extra,
            "unknown_role_ids": unknown_role_ids,
            "satisfied": len(missing) == 0,
            "summary": (
                "All required Microsoft Graph permissions are granted."
                if not missing
                else f"{len(missing)} of {len(required)} required permissions are missing."
            ),
        }
    except Exception as e:
        logger.error(f"Error validating app permissions for {client_id}: {str(e)}")
        return {"status": "error", "message": str(e)[:500]}
