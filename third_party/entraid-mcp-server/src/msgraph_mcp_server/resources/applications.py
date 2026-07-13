"""Applications resource module for Microsoft Graph.

This module provides access to Microsoft Graph application resources (app registrations).
"""

import logging
from typing import Dict, List, Any, Optional
from utils.graph_client import GraphClient
from msgraph.generated.models.application import Application
from .service_principals import get_service_principal_by_app_id

logger = logging.getLogger(__name__)

async def list_applications(graph_client: GraphClient, limit: int = 100) -> List[Dict[str, Any]]:
    """List all applications (app registrations) in the tenant, with paging.

    Each entry includes the registration's credentials (``passwordCredentials`` /
    ``keyCredentials``), configured API permissions (``requiredResourceAccess``) and
    ``owners`` (display names), so a single paged call yields enough for an app-registration
    inventory without an extra round-trip per app."""
    try:
        from msgraph.generated.applications.applications_request_builder import (
            ApplicationsRequestBuilder,
        )

        client = graph_client.get_client()
        # Expand owners so we don't need a per-app call; owners come back as directoryObjects.
        query_params = ApplicationsRequestBuilder.ApplicationsRequestBuilderGetQueryParameters(
            expand=["owners"],
            top=min(int(limit), 999),
        )
        request_configuration = ApplicationsRequestBuilder.ApplicationsRequestBuilderGetRequestConfiguration(
            query_parameters=query_params
        )
        response = await client.applications.get(request_configuration=request_configuration)
        applications = []
        if response and response.value:
            applications.extend(response.value)
        # Paging: fetch more if odata_next_link is present
        while response is not None and getattr(response, 'odata_next_link', None) and len(applications) < limit:
            response = await client.applications.with_url(response.odata_next_link).get()
            if response and response.value:
                applications.extend(response.value)
        formatted_apps = []
        for app in applications[:limit]:
            app_data = {
                'id': getattr(app, 'id', None),
                'appId': getattr(app, 'app_id', None),
                'displayName': getattr(app, 'display_name', None),
                'createdDateTime': app.created_date_time.isoformat() if getattr(app, 'created_date_time', None) else None,
                'signInAudience': getattr(app, 'sign_in_audience', None),
                'publisherDomain': getattr(app, 'publisher_domain', None),
                'tags': getattr(app, 'tags', None),
                'passwordCredentials': _format_credentials(getattr(app, 'password_credentials', None)),
                'keyCredentials': _format_credentials(getattr(app, 'key_credentials', None)),
                'requiredResourceAccess': _format_required_resource_access(getattr(app, 'required_resource_access', None)),
                'owners': _format_owners(getattr(app, 'owners', None)),
            }
            formatted_apps.append(app_data)
        return formatted_apps
    except Exception as e:
        logger.error(f"Error listing applications: {str(e)}")
        raise


def _format_credentials(creds) -> List[Dict[str, Any]]:
    """Project Graph password/key credential models to plain dicts (name + expiry)."""
    out: List[Dict[str, Any]] = []
    for c in creds or []:
        end_dt = getattr(c, "end_date_time", None)
        out.append({
            "displayName": getattr(c, "display_name", None),
            "keyId": str(getattr(c, "key_id", "") or ""),
            "endDateTime": end_dt.isoformat() if end_dt else None,
        })
    return out


def _format_required_resource_access(rras) -> List[Dict[str, Any]]:
    """Project ``requiredResourceAccess`` (resource app id + resource access GUIDs/types)."""
    out: List[Dict[str, Any]] = []
    for rra in rras or []:
        access = []
        for ra in (getattr(rra, "resource_access", None) or []):
            access.append({
                "id": str(getattr(ra, "id", "") or ""),
                "type": getattr(ra, "type", None),  # "Role" (Application) | "Scope" (Delegated)
            })
        out.append({
            "resourceAppId": getattr(rra, "resource_app_id", None),
            "resourceAccess": access,
        })
    return out


def _format_owners(owners) -> List[Dict[str, Any]]:
    """Project expanded owner directoryObjects to display name / UPN dicts."""
    out: List[Dict[str, Any]] = []
    for o in owners or []:
        out.append({
            "id": getattr(o, "id", None),
            "displayName": getattr(o, "display_name", None),
            "userPrincipalName": getattr(o, "user_principal_name", None),
        })
    return out



async def get_application_by_id(graph_client: GraphClient, app_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific application by its object ID, including appRoleAssignments and oauth2PermissionGrants from the corresponding service principal."""
    try:
        client = graph_client.get_client()
        app = await client.applications.by_application_id(app_id).get()
        if app:
            app_data = {
                'id': getattr(app, 'id', None),
                'appId': getattr(app, 'app_id', None),
                'displayName': getattr(app, 'display_name', None),
                'createdDateTime': app.created_date_time.isoformat() if getattr(app, 'created_date_time', None) else None,
                'signInAudience': getattr(app, 'sign_in_audience', None),
                'publisherDomain': getattr(app, 'publisher_domain', None),
                'tags': getattr(app, 'tags', None),
            }
            # Find the corresponding service principal by appId
            sp = await get_service_principal_by_app_id(graph_client, getattr(app, 'app_id', None))
            if sp:
                sp_id = getattr(sp, 'id', None)
                # Fetch appRoleAssignments and oauth2PermissionGrants using the same logic as in service_principals.py
                # Fetch appRoleAssignments
                app_role_assignments = []
                try:
                    response = await client.service_principals.by_service_principal_id(sp_id).app_role_assignments.get()
                    while response:
                        if response.value:
                            for assignment in response.value:
                                app_role_assignments.append({
                                    'id': getattr(assignment, 'id', None),
                                    'createdDateTime': getattr(assignment, 'created_date_time', None),
                                    'appRoleId': getattr(assignment, 'app_role_id', None),
                                    'principalDisplayName': getattr(assignment, 'principal_display_name', None),
                                    'principalId': getattr(assignment, 'principal_id', None),
                                    'principalType': getattr(assignment, 'principal_type', None),
                                    'resourceDisplayName': getattr(assignment, 'resource_display_name', None),
                                    'resourceId': getattr(assignment, 'resource_id', None),
                                })
                        if getattr(response, 'odata_next_link', None):
                            response = await client.service_principals.by_service_principal_id(sp_id).app_role_assignments.with_url(response.odata_next_link).get()
                        else:
                            break
                except Exception as e:
                    logger.warning(f"Error fetching appRoleAssignments for service principal {sp_id}: {str(e)}")
                app_data['appRoleAssignments'] = app_role_assignments

                # Fetch oauth2PermissionGrants
                oauth2_permission_grants = []
                try:
                    response = await client.service_principals.by_service_principal_id(sp_id).oauth2_permission_grants.get()
                    while response:
                        if response.value:
                            for grant in response.value:
                                oauth2_permission_grants.append({
                                    'id': getattr(grant, 'id', None),
                                    'clientId': getattr(grant, 'client_id', None),
                                    'consentType': getattr(grant, 'consent_type', None),
                                    'principalId': getattr(grant, 'principal_id', None),
                                    'resourceId': getattr(grant, 'resource_id', None),
                                    'scope': getattr(grant, 'scope', None),
                                })
                        if getattr(response, 'odata_next_link', None):
                            response = await client.service_principals.by_service_principal_id(sp_id).oauth2_permission_grants.with_url(response.odata_next_link).get()
                        else:
                            break
                except Exception as e:
                    logger.warning(f"Error fetching oauth2PermissionGrants for service principal {sp_id}: {str(e)}")
                app_data['oauth2PermissionGrants'] = oauth2_permission_grants
            else:
                app_data['appRoleAssignments'] = []
                app_data['oauth2PermissionGrants'] = []
            return app_data
        return None
    except Exception as e:
        logger.error(f"Error getting application {app_id}: {str(e)}")
        raise

async def create_application(graph_client: GraphClient, app_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new application (app registration)."""
    try:
        client = graph_client.get_client()
        app = Application()
        # Set properties from app_data
        if 'displayName' in app_data:
            app.display_name = app_data['displayName']
        if 'signInAudience' in app_data:
            app.sign_in_audience = app_data['signInAudience']
        if 'tags' in app_data:
            app.tags = app_data['tags']
        if 'identifierUris' in app_data:
            app.identifier_uris = app_data['identifierUris']
        if 'web' in app_data:
            app.web = app_data['web']
        if 'api' in app_data:
            app.api = app_data['api']
        if 'requiredResourceAccess' in app_data:
            app.required_resource_access = app_data['requiredResourceAccess']
        new_app = await client.applications.post(app)
        if new_app:
            return {
                'id': getattr(new_app, 'id', None),
                'appId': getattr(new_app, 'app_id', None),
                'displayName': getattr(new_app, 'display_name', None),
                'createdDateTime': new_app.created_date_time.isoformat() if getattr(new_app, 'created_date_time', None) else None,
                'signInAudience': getattr(new_app, 'sign_in_audience', None),
                'publisherDomain': getattr(new_app, 'publisher_domain', None),
                'tags': getattr(new_app, 'tags', None),
            }
        raise Exception("Failed to create application")
    except Exception as e:
        logger.error(f"Error creating application: {str(e)}")
        raise

async def update_application(graph_client: GraphClient, app_id: str, app_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing application (app registration)."""
    try:
        client = graph_client.get_client()
        app = Application()
        # Set updatable properties from app_data
        if 'displayName' in app_data:
            app.display_name = app_data['displayName']
        if 'signInAudience' in app_data:
            app.sign_in_audience = app_data['signInAudience']
        if 'tags' in app_data:
            app.tags = app_data['tags']
        if 'identifierUris' in app_data:
            app.identifier_uris = app_data['identifierUris']
        if 'web' in app_data:
            app.web = app_data['web']
        if 'api' in app_data:
            app.api = app_data['api']
        if 'requiredResourceAccess' in app_data:
            app.required_resource_access = app_data['requiredResourceAccess']
        await client.applications.by_application_id(app_id).patch(app)
        # Return the updated application
        return await get_application_by_id(graph_client, app_id)
    except Exception as e:
        logger.error(f"Error updating application {app_id}: {str(e)}")
        raise

async def delete_application(graph_client: GraphClient, app_id: str) -> bool:
    """Delete an application (app registration) by its object ID."""
    try:
        client = graph_client.get_client()
        await client.applications.by_application_id(app_id).delete()
        return True
    except Exception as e:
        logger.error(f"Error deleting application {app_id}: {str(e)}")
        raise


def _days_until(end_dt) -> Optional[int]:
    """Whole days from now until ``end_dt`` (negative if already expired)."""
    if not end_dt:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    return int((end_dt - now).total_seconds() // 86400)


def _collect_creds(owner_type: str, owner, within_days: int, include_expired: bool):
    """Yield expiring/expired secret + certificate credentials for an app or SP."""
    out = []
    for kind, attr in (("secret", "password_credentials"), ("certificate", "key_credentials")):
        for cred in (getattr(owner, attr, None) or []):
            end_dt = getattr(cred, "end_date_time", None)
            days = _days_until(end_dt)
            if days is None:
                continue
            if days < 0 and not include_expired:
                continue
            if days > within_days:
                continue
            out.append({
                "ownerType": owner_type,
                "ownerId": getattr(owner, "id", None),
                "appId": getattr(owner, "app_id", None),
                "displayName": getattr(owner, "display_name", None),
                "credentialType": kind,
                "credentialName": getattr(cred, "display_name", None),
                "keyId": str(getattr(cred, "key_id", "") or ""),
                "endDateTime": end_dt.isoformat() if end_dt else None,
                "daysUntilExpiry": days,
                "status": "expired" if days < 0 else "expiring",
            })
    return out


async def find_expiring_credentials(
    graph_client: GraphClient,
    within_days: int = 30,
    include_expired: bool = True,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Find application registrations AND service principals whose client secrets or
    certificates are expired or expire within ``within_days`` days. Results are sorted
    soonest-to-expire first."""
    client = graph_client.get_client()
    findings: List[Dict[str, Any]] = []

    # Applications (app registrations) — secrets + certs live on the application object.
    response = await client.applications.get()
    apps = list(response.value) if (response and response.value) else []
    while response is not None and getattr(response, "odata_next_link", None) and len(apps) < limit:
        response = await client.applications.with_url(response.odata_next_link).get()
        if response and response.value:
            apps.extend(response.value)
    for app in apps[:limit]:
        findings.extend(_collect_creds("application", app, within_days, include_expired))

    # Service principals — may carry their own credentials too.
    sp_response = await client.service_principals.get()
    sps = list(sp_response.value) if (sp_response and sp_response.value) else []
    while sp_response is not None and getattr(sp_response, "odata_next_link", None) and len(sps) < limit:
        sp_response = await client.service_principals.with_url(sp_response.odata_next_link).get()
        if sp_response and sp_response.value:
            sps.extend(sp_response.value)
    for sp in sps[:limit]:
        findings.extend(_collect_creds("servicePrincipal", sp, within_days, include_expired))

    findings.sort(key=lambda f: f["daysUntilExpiry"])
    return findings


async def find_ownerless_applications(
    graph_client: GraphClient,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return application registrations that have NO assigned owners.

    Ownerless app registrations are a governance/identity risk: nobody is accountable
    for rotating their credentials or decommissioning them. For each application this
    fetches ``/applications/{id}/owners`` and returns only those with zero owners.
    Requires Application.Read.All / Directory.Read.All."""
    client = graph_client.get_client()
    response = await client.applications.get()
    apps = list(response.value) if (response and response.value) else []
    while response is not None and getattr(response, "odata_next_link", None) and len(apps) < limit:
        response = await client.applications.with_url(response.odata_next_link).get()
        if response and response.value:
            apps.extend(response.value)

    out: List[Dict[str, Any]] = []
    for app in apps[:limit]:
        obj_id = getattr(app, "id", None)
        if not obj_id:
            continue
        try:
            owners_resp = await client.applications.by_application_id(obj_id).owners.get()
            owner_count = len(owners_resp.value) if (owners_resp and owners_resp.value) else 0
        except Exception as e:  # noqa: BLE001 - treat an owners read error as unknown, skip
            logger.warning(f"Error reading owners for application {obj_id}: {str(e)}")
            continue
        if owner_count == 0:
            out.append({
                "id": obj_id,
                "appId": getattr(app, "app_id", None),
                "displayName": getattr(app, "display_name", None),
                "createdDateTime": app.created_date_time.isoformat() if getattr(app, "created_date_time", None) else None,
                "signInAudience": getattr(app, "sign_in_audience", None),
            })
    return out
