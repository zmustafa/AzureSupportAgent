"""User resource module for Microsoft Graph.

This module provides access to Microsoft Graph user resources.
"""

import logging
from typing import Dict, List, Optional, Any

from msgraph.generated.users.users_request_builder import UsersRequestBuilder
from kiota_abstractions.base_request_configuration import RequestConfiguration
from msgraph.generated.directory_roles.directory_roles_request_builder import DirectoryRolesRequestBuilder
from msgraph.generated.directory_roles.item.directory_role_item_request_builder import DirectoryRoleItemRequestBuilder
from msgraph.generated.directory_roles.item.members.members_request_builder import MembersRequestBuilder

from utils.graph_client import GraphClient

logger = logging.getLogger(__name__)

async def search_users(graph_client: GraphClient, query: str, limit: int = 10) -> List[Dict[str, str]]:
    """Search for users by name or email, with paging support.

    An empty/whitespace query lists users (no $search) instead of returning a 400 —
    callers often use search_users with a blank query to mean "show me some users".
    """
    try:
        client = graph_client.get_client()
        q = (query or "").strip()
        if not q:
            # No search term: just list the first page of users.
            list_params = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(top=limit)
            list_config = UsersRequestBuilder.UsersRequestBuilderGetRequestConfiguration(
                query_parameters=list_params
            )
            response = await client.users.get(request_configuration=list_config)
        else:
            # Create query parameters for the search
            query_params = UsersRequestBuilder.UsersRequestBuilderGetQueryParameters(
                search=[
                    f'(\"displayName:{q}\" OR \"mail:{q}\" OR \"userPrincipalName:{q}\" OR \"givenName:{q}\" OR \"surName:{q}\" OR \"otherMails:{q}\")'
                ],
                top=limit
            )
            # Create request configuration
            request_configuration = UsersRequestBuilder.UsersRequestBuilderGetRequestConfiguration(
                query_parameters=query_params
            )
            request_configuration.headers.add("ConsistencyLevel", "eventual")
            # Execute the search
            response = await client.users.get(request_configuration=request_configuration)
        users = []
        if response and response.value:
            users.extend(response.value)
        # Paging: fetch more if odata_next_link is present, but stop if we reach the limit
        while response is not None and getattr(response, 'odata_next_link', None) and len(users) < limit:
            response = await client.users.with_url(response.odata_next_link).get()
            if response and response.value:
                users.extend(response.value)
        # Format the response with all user fields
        formatted_users = []
        for user in users[:limit]:
            user_data = {
                'id': user.id,
                'displayName': user.display_name,
                'mail': user.mail,
                'userPrincipalName': user.user_principal_name,
                'givenName': user.given_name,
                'surname': user.surname,
                'jobTitle': user.job_title,
                'officeLocation': user.office_location,
                'businessPhones': user.business_phones,
                'mobilePhone': user.mobile_phone
            }
            formatted_users.append(user_data)
        return formatted_users
    except Exception as e:
        logger.error(f"Error searching users: {str(e)}")
        raise

async def get_user_by_id(graph_client: GraphClient, user_id: str) -> Optional[Dict[str, Any]]:
    """Get a user by their ID.
    
    Args:
        graph_client: GraphClient instance
        user_id: The unique identifier of the user.
        
    Returns:
        A dictionary containing the user's details if found, otherwise None.
    """
    try:
        client = graph_client.get_client()
        ms_user = await client.users.by_user_id(user_id).get()
        
        if ms_user:
            # Convert MS Graph User to our dictionary format
            user_data = {
                'id': ms_user.id,
                'displayName': ms_user.display_name,
                'mail': ms_user.mail,
                'userPrincipalName': ms_user.user_principal_name,
                'givenName': ms_user.given_name,
                'surname': ms_user.surname,
                'jobTitle': ms_user.job_title,
                'officeLocation': ms_user.office_location,
                'businessPhones': ms_user.business_phones,
                'mobilePhone': ms_user.mobile_phone
            }
            return user_data
        else:
            logger.warning(f"User with ID {user_id} not found.")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching user with ID {user_id}: {str(e)}")
        raise

async def get_privileged_users(graph_client: GraphClient) -> List[Dict[str, Any]]:
    """Get all users who are members of privileged directory roles, with paging support for members."""
    try:
        client = graph_client.get_client()
        # Get all activated directory roles
        roles_response = await client.directory_roles.get()
        privileged_users = {}
        if roles_response and roles_response.value:
            for role in roles_response.value:
                # For each role, get its members (with paging)
                role_id = role.id
                role_name = getattr(role, 'display_name', None)
                if not role_id:
                    continue
                members_response = await client.directory_roles.by_directory_role_id(role_id).members.get()
                members = []
                if members_response and members_response.value:
                    members.extend(members_response.value)
                while members_response is not None and getattr(members_response, 'odata_next_link', None):
                    members_response = await client.directory_roles.by_directory_role_id(role_id).members.with_url(members_response.odata_next_link).get()
                    if members_response and members_response.value:
                        members.extend(members_response.value)
                for member in members:
                    # Only process user objects (type: #microsoft.graph.user)
                    if hasattr(member, 'odata_type') and member.odata_type == '#microsoft.graph.user':
                        user_id = getattr(member, 'id', None)
                        if not user_id:
                            continue
                        # Deduplicate by user_id
                        if user_id not in privileged_users:
                            privileged_users[user_id] = {
                                'id': user_id,
                                'displayName': getattr(member, 'display_name', None),
                                'mail': getattr(member, 'mail', None),
                                'userPrincipalName': getattr(member, 'user_principal_name', None),
                                'givenName': getattr(member, 'given_name', None),
                                'surname': getattr(member, 'surname', None),
                                'jobTitle': getattr(member, 'job_title', None),
                                'officeLocation': getattr(member, 'office_location', None),
                                'businessPhones': getattr(member, 'business_phones', None),
                                'mobilePhone': getattr(member, 'mobile_phone', None),
                                'roles': set()
                            }
                        # Add the role name to the user's roles set
                        privileged_users[user_id]['roles'].add(role_name)
        # Convert roles set to list for each user
        for user in privileged_users.values():
            user['roles'] = list(user['roles'])
        return list(privileged_users.values())
    except Exception as e:
        logger.error(f"Error fetching privileged users: {str(e)}")
        raise

async def get_user_groups(graph_client: GraphClient, user_id: str) -> List[Dict[str, Any]]:
    """Get all groups (including transitive memberships) for a user by user ID, with paging support."""
    try:
        client = graph_client.get_client()
        memberships_response = await client.users.by_user_id(user_id).transitive_member_of.get()
        memberships = []
        if memberships_response and memberships_response.value:
            memberships.extend(memberships_response.value)
        # Paging for memberships
        while memberships_response is not None and getattr(memberships_response, 'odata_next_link', None):
            memberships_response = await client.users.by_user_id(user_id).transitive_member_of.with_url(memberships_response.odata_next_link).get()
            if memberships_response and memberships_response.value:
                memberships.extend(memberships_response.value)
        # For each membership, fetch group details if it is a group
        groups_list = []
        for membership in memberships:
            if hasattr(membership, 'odata_type') and membership.odata_type == '#microsoft.graph.group':
                group_id = getattr(membership, 'id', None)
                if not group_id:
                    continue
                group = await client.groups.by_group_id(group_id).get()
                if group:
                    group_data = {
                        'id': group.id,
                        'displayName': getattr(group, 'display_name', None),
                        'mail': getattr(group, 'mail', None),
                        'groupTypes': getattr(group, 'group_types', None),
                        'description': getattr(group, 'description', None)
                    }
                    groups_list.append(group_data)
        return groups_list
    except Exception as e:
        logger.error(f"Error fetching groups for user {user_id}: {str(e)}")
        raise

async def get_user_roles(graph_client: GraphClient, user_id: str) -> List[Dict[str, Any]]:
    """Get all directory roles assigned to a user by user ID, with paging support."""
    try:
        client = graph_client.get_client()
        memberof_response = await client.users.by_user_id(user_id).member_of.get()
        memberships = []
        if memberof_response and memberof_response.value:
            memberships.extend(memberof_response.value)
        # Paging for memberOf
        while memberof_response is not None and getattr(memberof_response, 'odata_next_link', None):
            memberof_response = await client.users.by_user_id(user_id).member_of.with_url(memberof_response.odata_next_link).get()
            if memberof_response and memberof_response.value:
                memberships.extend(memberof_response.value)
        # For each membership, filter for directoryRole objects
        roles_list = []
        for membership in memberships:
            if hasattr(membership, 'odata_type') and membership.odata_type == '#microsoft.graph.directoryRole':
                role_id = getattr(membership, 'id', None)
                if not role_id:
                    continue
                # Optionally fetch more details if needed
                role = await client.directory_roles.by_directory_role_id(role_id).get()
                if role:
                    role_data = {
                        'id': role.id,
                        'displayName': getattr(role, 'display_name', None),
                        'description': getattr(role, 'description', None),
                        'roleTemplateId': getattr(role, 'role_template_id', None)
                    }
                    roles_list.append(role_data)
        return roles_list
    except Exception as e:
        logger.error(f"Error fetching roles for user {user_id}: {str(e)}")
        raise
