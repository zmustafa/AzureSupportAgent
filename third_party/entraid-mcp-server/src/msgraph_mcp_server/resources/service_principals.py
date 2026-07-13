"""Service Principals resource module for Microsoft Graph.

This module provides access to Microsoft Graph service principal resources.
"""

import logging
from typing import Dict, List, Any, Optional
from utils.graph_client import GraphClient
from msgraph.generated.models.service_principal import ServicePrincipal

logger = logging.getLogger(__name__)

async def list_service_principals(graph_client: GraphClient, limit: int = 100) -> List[Dict[str, Any]]:
    """List all service principals in the tenant, with paging."""
    try:
        client = graph_client.get_client()
        response = await client.service_principals.get()
        service_principals = []
        if response and response.value:
            service_principals.extend(response.value)
        # Paging: fetch more if odata_next_link is present
        while response is not None and getattr(response, 'odata_next_link', None) and len(service_principals) < limit:
            response = await client.service_principals.with_url(response.odata_next_link).get()
            if response and response.value:
                service_principals.extend(response.value)
        formatted_sps = []
        for sp in service_principals[:limit]:
            sp_data = {
                'id': getattr(sp, 'id', None),
                'appId': getattr(sp, 'app_id', None),
                'displayName': getattr(sp, 'display_name', None),
                'createdDateTime': sp.created_date_time.isoformat() if getattr(sp, 'created_date_time', None) else None,
                'accountEnabled': getattr(sp, 'account_enabled', None),
                'appOwnerOrganizationId': getattr(sp, 'app_owner_organization_id', None),
                'tags': getattr(sp, 'tags', None),
            }
            formatted_sps.append(sp_data)
        return formatted_sps
    except Exception as e:
        logger.error(f"Error listing service principals: {str(e)}")
        raise

async def get_service_principal_by_app_id(graph_client: GraphClient, app_id: str) -> Optional[Any]:
    """Get a service principal by its appId (application client ID)."""
    try:
        client = graph_client.get_client()
        # Filter by appId. The kiota SDK requires a typed RequestConfiguration; passing
        # query_parameters straight to .get() raises "unexpected keyword argument".
        from msgraph.generated.service_principals.service_principals_request_builder import (
            ServicePrincipalsRequestBuilder,
        )
        query_params = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetQueryParameters(
            filter=f"appId eq '{app_id}'"
        )
        request_configuration = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetRequestConfiguration(
            query_parameters=query_params
        )
        response = await client.service_principals.get(request_configuration=request_configuration)
        if response and response.value:
            return response.value[0]  # Return the first match
        return None
    except Exception as e:
        logger.error(f"Error getting service principal by appId {app_id}: {str(e)}")
        raise

async def get_service_principal_by_id(graph_client: GraphClient, sp_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific service principal by its object ID, including appRoleAssignments and oauth2PermissionGrants.

    Callers (and LLMs) frequently pass the application **appId** (client id) instead of
    the service-principal **object id**; the object-id lookup then 404s. To be robust we
    fall back to resolving the SP by its appId filter and continue with the real object id.
    """
    try:
        client = graph_client.get_client()
        try:
            sp = await client.service_principals.by_service_principal_id(sp_id).get()
        except Exception as first_err:  # noqa: BLE001
            if "404" in str(first_err) or "ResourceNotFound" in str(first_err):
                logger.info(f"SP object-id lookup for {sp_id} failed; retrying by appId.")
                sp = await get_service_principal_by_app_id(graph_client, sp_id)
            else:
                raise
        if sp:
            # From here on, use the resolved SP's real object id for sub-resource calls.
            sp_id = getattr(sp, 'id', None) or sp_id
            sp_data = {
                'id': getattr(sp, 'id', None),
                'appId': getattr(sp, 'app_id', None),
                'displayName': getattr(sp, 'display_name', None),
                'createdDateTime': sp.created_date_time.isoformat() if getattr(sp, 'created_date_time', None) else None,
                'accountEnabled': getattr(sp, 'account_enabled', None),
                'appOwnerOrganizationId': getattr(sp, 'app_owner_organization_id', None),
                'tags': getattr(sp, 'tags', None),
            }
            # Fetch appRoleAssignments (application permissions)
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
            sp_data['appRoleAssignments'] = app_role_assignments

            # Fetch oauth2PermissionGrants (delegated permissions)
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
            sp_data['oauth2PermissionGrants'] = oauth2_permission_grants

            return sp_data
        return None
    except Exception as e:
        logger.error(f"Error getting service principal {sp_id}: {str(e)}")
        raise

async def create_service_principal(graph_client: GraphClient, sp_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new service principal."""
    try:
        client = graph_client.get_client()
        sp = ServicePrincipal()
        # Set properties from sp_data
        if 'appId' in sp_data:
            sp.app_id = sp_data['appId']
        if 'accountEnabled' in sp_data:
            sp.account_enabled = sp_data['accountEnabled']
        if 'tags' in sp_data:
            sp.tags = sp_data['tags']
        if 'appRoleAssignmentRequired' in sp_data:
            sp.app_role_assignment_required = sp_data['appRoleAssignmentRequired']
        if 'displayName' in sp_data:
            sp.display_name = sp_data['displayName']
        new_sp = await client.service_principals.post(sp)
        if new_sp:
            return {
                'id': getattr(new_sp, 'id', None),
                'appId': getattr(new_sp, 'app_id', None),
                'displayName': getattr(new_sp, 'display_name', None),
                'createdDateTime': new_sp.created_date_time.isoformat() if getattr(new_sp, 'created_date_time', None) else None,
                'accountEnabled': getattr(new_sp, 'account_enabled', None),
                'appOwnerOrganizationId': getattr(new_sp, 'app_owner_organization_id', None),
                'tags': getattr(new_sp, 'tags', None),
            }
        raise Exception("Failed to create service principal")
    except Exception as e:
        logger.error(f"Error creating service principal: {str(e)}")
        raise

async def update_service_principal(graph_client: GraphClient, sp_id: str, sp_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing service principal."""
    try:
        client = graph_client.get_client()
        sp = ServicePrincipal()
        # Set updatable properties from sp_data
        if 'accountEnabled' in sp_data:
            sp.account_enabled = sp_data['accountEnabled']
        if 'tags' in sp_data:
            sp.tags = sp_data['tags']
        if 'appRoleAssignmentRequired' in sp_data:
            sp.app_role_assignment_required = sp_data['appRoleAssignmentRequired']
        if 'displayName' in sp_data:
            sp.display_name = sp_data['displayName']
        await client.service_principals.by_service_principal_id(sp_id).patch(sp)
        # Return the updated service principal
        return await get_service_principal_by_id(graph_client, sp_id)
    except Exception as e:
        logger.error(f"Error updating service principal {sp_id}: {str(e)}")
        raise

async def delete_service_principal(graph_client: GraphClient, sp_id: str) -> bool:
    """Delete a service principal by its object ID."""
    try:
        client = graph_client.get_client()
        await client.service_principals.by_service_principal_id(sp_id).delete()
        return True
    except Exception as e:
        logger.error(f"Error deleting service principal {sp_id}: {str(e)}")
        raise 