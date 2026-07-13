"""Groups resource module for Microsoft Graph.

This module provides access to Microsoft Graph group resources.
"""

import logging
from typing import Dict, List, Any, Optional
from msgraph.generated.groups.groups_request_builder import GroupsRequestBuilder
from msgraph.generated.models.group import Group
from msgraph.generated.models.directory_object import DirectoryObject
from utils.graph_client import GraphClient

logger = logging.getLogger(__name__)

async def get_all_groups(graph_client: GraphClient, limit: int = 100) -> List[Dict[str, Any]]:
    """Get all groups (up to the specified limit, with paging)."""
    try:
        client = graph_client.get_client()
        query_params = GroupsRequestBuilder.GroupsRequestBuilderGetQueryParameters(top=limit)
        request_configuration = GroupsRequestBuilder.GroupsRequestBuilderGetRequestConfiguration(query_parameters=query_params)
        response = await client.groups.get(request_configuration=request_configuration)
        groups = []
        if response and response.value:
            groups.extend(response.value)
        # Paging: fetch more if odata_next_link is present
        while response is not None and getattr(response, 'odata_next_link', None):
            response = await client.groups.with_url(response.odata_next_link).get()
            if response and response.value:
                groups.extend(response.value)
        # Format output
        formatted_groups = []
        for group in groups[:limit]:
            group_data = {
                'id': group.id,
                'displayName': group.display_name,
                'mail': group.mail,
                'mailNickname': group.mail_nickname,
                'description': group.description,
                'groupTypes': group.group_types,
                'securityEnabled': group.security_enabled,
                'mailEnabled': group.mail_enabled,
                'createdDateTime': group.created_date_time.isoformat() if group.created_date_time else None
            }
            formatted_groups.append(group_data)
        return formatted_groups
    except Exception as e:
        logger.error(f"Error fetching all groups: {str(e)}")
        raise

async def get_group_by_id(graph_client: GraphClient, group_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific group by ID."""
    try:
        client = graph_client.get_client()
        group = await client.groups.by_group_id(group_id).get()
        
        if group:
            group_data = {
                'id': group.id,
                'displayName': group.display_name,
                'mail': group.mail,
                'mailNickname': group.mail_nickname,
                'description': group.description,
                'groupTypes': group.group_types,
                'securityEnabled': group.security_enabled,
                'mailEnabled': group.mail_enabled,
                'visibility': group.visibility,
                'createdDateTime': group.created_date_time.isoformat() if group.created_date_time else None
            }
            return group_data
        return None
    except Exception as e:
        logger.error(f"Error fetching group {group_id}: {str(e)}")
        raise

async def search_groups_by_name(graph_client: GraphClient, name: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Search for groups by display name (case-insensitive, partial match, with paging)."""
    try:
        client = graph_client.get_client()
        filter_query = f"startswith(displayName,'{name}')"
        query_params = GroupsRequestBuilder.GroupsRequestBuilderGetQueryParameters(
            filter=filter_query, top=limit
        )
        request_configuration = GroupsRequestBuilder.GroupsRequestBuilderGetRequestConfiguration(query_parameters=query_params)
        response = await client.groups.get(request_configuration=request_configuration)
        groups = []
        if response and response.value:
            groups.extend(response.value)
        # Paging
        while response is not None and getattr(response, 'odata_next_link', None):
            response = await client.groups.with_url(response.odata_next_link).get()
            if response and response.value:
                groups.extend(response.value)
        formatted_groups = []
        for group in groups[:limit]:
            group_data = {
                'id': group.id,
                'displayName': group.display_name,
                'mail': group.mail,
                'mailNickname': group.mail_nickname,
                'description': group.description,
                'groupTypes': group.group_types,
                'securityEnabled': group.security_enabled,
                'mailEnabled': group.mail_enabled,
                'createdDateTime': group.created_date_time.isoformat() if group.created_date_time else None
            }
            formatted_groups.append(group_data)
        return formatted_groups
    except Exception as e:
        logger.error(f"Error searching groups by name: {str(e)}")
        raise

async def get_group_members(graph_client: GraphClient, group_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Get members of a group by group ID (up to the specified limit, with paging)."""
    try:
        client = graph_client.get_client()
        members_response = await client.groups.by_group_id(group_id).members.get()
        members = []
        if members_response and members_response.value:
            members.extend(members_response.value)
        # Paging
        while members_response is not None and getattr(members_response, 'odata_next_link', None):
            members_response = await client.groups.by_group_id(group_id).members.with_url(members_response.odata_next_link).get()
            if members_response and members_response.value:
                members.extend(members_response.value)
        formatted_members = []
        for member in members[:limit]:
            member_data = {
                'id': getattr(member, 'id', None),
                'displayName': getattr(member, 'display_name', None),
                'mail': getattr(member, 'mail', None),
                'userPrincipalName': getattr(member, 'user_principal_name', None),
                'givenName': getattr(member, 'given_name', None),
                'surname': getattr(member, 'surname', None),
                'jobTitle': getattr(member, 'job_title', None),
                'officeLocation': getattr(member, 'office_location', None),
                'businessPhones': getattr(member, 'business_phones', None),
                'mobilePhone': getattr(member, 'mobile_phone', None),
                'type': getattr(member, 'odata_type', None)
            }
            formatted_members.append(member_data)
        return formatted_members
    except Exception as e:
        logger.error(f"Error fetching group members for group {group_id}: {str(e)}")
        raise

async def create_group(graph_client: GraphClient, group_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new group in Microsoft Graph.
    
    Args:
        graph_client: GraphClient instance
        group_data: Dictionary containing group properties
        
    Returns:
        The created group data
    """
    try:
        client = graph_client.get_client()
        
        # Check if group already exists with the same display name or mail nickname
        display_name = group_data.get('displayName')
        mail_nickname = group_data.get('mailNickname')
        
        if display_name:
            # Check if a group with the same display name already exists
            filter_query = f"displayName eq '{display_name}'"
            query_params = GroupsRequestBuilder.GroupsRequestBuilderGetQueryParameters(
                filter=filter_query
            )
            request_configuration = GroupsRequestBuilder.GroupsRequestBuilderGetRequestConfiguration(query_parameters=query_params)
            response = await client.groups.get(request_configuration=request_configuration)
            
            if response and response.value and len(response.value) > 0:
                logger.info(f"Group with display name '{display_name}' already exists")
                # Return the existing group
                existing_group = response.value[0]
                return {
                    'id': existing_group.id,
                    'displayName': existing_group.display_name,
                    'mail': existing_group.mail,
                    'mailNickname': existing_group.mail_nickname,
                    'description': existing_group.description,
                    'groupTypes': existing_group.group_types,
                    'securityEnabled': existing_group.security_enabled,
                    'mailEnabled': existing_group.mail_enabled,
                    'visibility': existing_group.visibility,
                    'createdDateTime': existing_group.created_date_time.isoformat() if existing_group.created_date_time else None,
                    'status': 'already_exists'
                }
        
        # Create a group object with the provided data
        group = Group()
        
        # Set required properties
        if 'displayName' in group_data:
            group.display_name = group_data['displayName']
        else:
            raise ValueError("displayName is required for creating a group")
            
        if 'mailNickname' in group_data:
            group.mail_nickname = group_data['mailNickname']
        else:
            raise ValueError("mailNickname is required for creating a group")
        
        # Set optional properties
        if 'description' in group_data:
            group.description = group_data['description']
        
        # Handle group types and dynamic membership
        is_dynamic = False
        if 'groupTypes' in group_data:
            group_types = group_data['groupTypes']
            
            # Check if DynamicMembership is in the group types
            if 'DynamicMembership' in group_types:
                is_dynamic = True
                
                # For dynamic groups, membershipRule and membershipRuleProcessingState are required
                if 'membershipRule' not in group_data:
                    raise ValueError("membershipRule is required for dynamic membership groups")
                
                group.membership_rule = group_data['membershipRule']
                group.membership_rule_processing_state = group_data.get('membershipRuleProcessingState', 'On')
                
            group.group_types = group_types
        
        if 'mailEnabled' in group_data:
            group.mail_enabled = group_data['mailEnabled']
        
        if 'securityEnabled' in group_data:
            group.security_enabled = group_data['securityEnabled']
        
        if 'visibility' in group_data:
            group.visibility = group_data['visibility']
            
        if 'owners' in group_data:
            if not isinstance(group_data['owners'], list):
                raise ValueError("owners must be a list of user IDs")
        
        if 'members' in group_data and not is_dynamic:
            # Members cannot be added during creation for dynamic groups
            if not isinstance(group_data['members'], list):
                raise ValueError("members must be a list of user IDs")
        
        # Create the group
        new_group = await client.groups.post(group)
        
        # Add owners if provided
        if 'owners' in group_data and new_group and new_group.id:
            for owner_id in group_data['owners']:
                await add_group_owner(graph_client, new_group.id, owner_id)
        
        # Add members if provided and not dynamic membership
        if 'members' in group_data and new_group and new_group.id and not is_dynamic:
            for member_id in group_data['members']:
                await add_group_member(graph_client, new_group.id, member_id)
        
        # Return the created group
        if new_group:
            created_group = {
                'id': new_group.id,
                'displayName': new_group.display_name,
                'mail': new_group.mail,
                'mailNickname': new_group.mail_nickname,
                'description': new_group.description,
                'groupTypes': new_group.group_types,
                'securityEnabled': new_group.security_enabled,
                'mailEnabled': new_group.mail_enabled,
                'visibility': new_group.visibility,
                'createdDateTime': new_group.created_date_time.isoformat() if new_group.created_date_time else None
            }
            
            # Add dynamic membership properties if applicable
            if is_dynamic:
                created_group['membershipRule'] = new_group.membership_rule
                created_group['membershipRuleProcessingState'] = new_group.membership_rule_processing_state
                
            return created_group
        
        raise Exception("Failed to create group")
    except Exception as e:
        logger.error(f"Error creating group: {str(e)}")
        raise

async def update_group(graph_client: GraphClient, group_id: str, group_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing group in Microsoft Graph.
    
    Args:
        graph_client: GraphClient instance
        group_id: ID of the group to update
        group_data: Dictionary containing group properties to update
        
    Returns:
        The updated group data
    """
    try:
        client = graph_client.get_client()
        
        # Create a group object with the provided update data
        group = Group()
        
        # Set properties to update
        if 'displayName' in group_data:
            group.display_name = group_data['displayName']
        
        if 'mailNickname' in group_data:
            group.mail_nickname = group_data['mailNickname']
        
        if 'description' in group_data:
            group.description = group_data['description']
        
        if 'visibility' in group_data:
            group.visibility = group_data['visibility']
        
        # Update the group
        await client.groups.by_group_id(group_id).patch(group)
        
        # Get the updated group to return
        updated_group = await get_group_by_id(graph_client, group_id)
        if not updated_group:
            raise Exception(f"Failed to retrieve updated group with ID {group_id}")
            
        return updated_group
    except Exception as e:
        logger.error(f"Error updating group {group_id}: {str(e)}")
        raise

async def delete_group(graph_client: GraphClient, group_id: str) -> bool:
    """Delete a group from Microsoft Graph.
    
    Args:
        graph_client: GraphClient instance
        group_id: ID of the group to delete
        
    Returns:
        True if successful, raises an exception otherwise
    """
    try:
        client = graph_client.get_client()
        
        # Delete the group
        await client.groups.by_group_id(group_id).delete()
        
        return True
    except Exception as e:
        logger.error(f"Error deleting group {group_id}: {str(e)}")
        raise

async def add_group_member(graph_client: GraphClient, group_id: str, member_id: str) -> bool:
    """Add a member to a group.
    
    Args:
        graph_client: GraphClient instance
        group_id: ID of the group
        member_id: ID of the member (user, group, device, etc.) to add
        
    Returns:
        True if successful, raises an exception otherwise
    """
    try:
        client = graph_client.get_client()
        
        # First, check if the group is dynamic - can't add members to dynamic groups
        group = await client.groups.by_group_id(group_id).get()
        if group and group.group_types and 'DynamicMembership' in group.group_types:
            logger.warning(f"Cannot add members to dynamic group {group_id}")
            raise ValueError("Cannot add members to a dynamic membership group. Members are determined by the membership rule.")
        
        # Check if member is already in the group
        try:
            # This will raise an exception if member is not found
            existing_member = await client.groups.by_group_id(group_id).members.by_directory_object_id(member_id).get()
            if existing_member:
                logger.info(f"Member {member_id} is already in group {group_id}")
                return True
        except Exception:
            # Member is not in the group, continue with adding
            pass
        
        # Create a reference to the directory object (member)
        directory_object = DirectoryObject()
        directory_object.id = member_id
        
        # Add the member to the group
        await client.groups.by_group_id(group_id).members.ref.post(directory_object)
        
        return True
    except Exception as e:
        logger.error(f"Error adding member {member_id} to group {group_id}: {str(e)}")
        raise

async def remove_group_member(graph_client: GraphClient, group_id: str, member_id: str) -> bool:
    """Remove a member from a group.
    
    Args:
        graph_client: GraphClient instance
        group_id: ID of the group
        member_id: ID of the member to remove
        
    Returns:
        True if successful, raises an exception otherwise
    """
    try:
        client = graph_client.get_client()
        
        # First, check if the group is dynamic - can't remove members from dynamic groups
        group = await client.groups.by_group_id(group_id).get()
        if group and group.group_types and 'DynamicMembership' in group.group_types:
            logger.warning(f"Cannot remove members from dynamic group {group_id}")
            raise ValueError("Cannot remove members from a dynamic membership group. Members are determined by the membership rule.")
        
        # Check if member exists in the group
        try:
            # This will raise an exception if member is not found
            await client.groups.by_group_id(group_id).members.by_directory_object_id(member_id).get()
        except Exception as e:
            logger.info(f"Member {member_id} not found in group {group_id}: {str(e)}")
            return True  # Already not a member, so removal "succeeded"
        
        # Remove the member from the group
        await client.groups.by_group_id(group_id).members.by_directory_object_id(member_id).ref.delete()
        
        return True
    except Exception as e:
        logger.error(f"Error removing member {member_id} from group {group_id}: {str(e)}")
        raise

async def add_group_owner(graph_client: GraphClient, group_id: str, owner_id: str) -> bool:
    """Add an owner to a group.
    
    Args:
        graph_client: GraphClient instance
        group_id: ID of the group
        owner_id: ID of the user to add as owner
        
    Returns:
        True if successful, raises an exception otherwise
    """
    try:
        client = graph_client.get_client()
        
        # Create a reference to the directory object (owner)
        directory_object = DirectoryObject()
        directory_object.id = owner_id
        
        # Add the owner to the group
        await client.groups.by_group_id(group_id).owners.ref.post(directory_object)
        
        return True
    except Exception as e:
        logger.error(f"Error adding owner {owner_id} to group {group_id}: {str(e)}")
        raise

async def remove_group_owner(graph_client: GraphClient, group_id: str, owner_id: str) -> bool:
    """Remove an owner from a group.
    
    Args:
        graph_client: GraphClient instance
        group_id: ID of the group
        owner_id: ID of the owner to remove
        
    Returns:
        True if successful, raises an exception otherwise
    """
    try:
        client = graph_client.get_client()
        
        # Remove the owner from the group
        await client.groups.by_group_id(group_id).owners.by_directory_object_id(owner_id).ref.delete()
        
        return True
    except Exception as e:
        logger.error(f"Error removing owner {owner_id} from group {group_id}: {str(e)}")
        raise 