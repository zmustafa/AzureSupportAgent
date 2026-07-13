"""Microsoft Graph MCP Server.

This module provides the main FastMCP server implementation for
interacting with Microsoft Graph services.
"""

import logging
from typing import Dict, List, Optional, Any
from fastmcp import FastMCP, Context

from auth.graph_auth import GraphAuthManager, AuthenticationError
from utils.graph_client import GraphClient
from utils.password_generator import generate_secure_password
from resources import users, signin_logs, mfa, conditional_access, groups, managed_devices, audit_logs, password_auth, permissions_helper, applications, service_principals

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Create an MCP server
mcp = FastMCP("EntraID MCP Server")

# Initialize Graph client
try:
    auth_manager = GraphAuthManager()
    graph_client = GraphClient(auth_manager)
    logger.info("Successfully initialized Graph client")
except AuthenticationError as e:
    logger.error(f"Failed to initialize Graph client: {str(e)}")
    raise

@mcp.tool()
async def search_users(query: str, ctx: Context, limit: int = 10) -> List[Dict[str, Any]]:
    """Search for users by name or email.
    
    Args:
        query: Search query (name or email)
        ctx: Context object
        limit: Maximum number of results to return (default: 10)
    """
    await ctx.info(f"Searching for users matching '{query}'...")
    
    try:
        results = await users.search_users(graph_client, query, limit)
        await ctx.report_progress(progress=100, total=100)
        return results
    except AuthenticationError as e:
        error_msg = f"Authentication error: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Error searching users: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_user_by_id(user_id: str, ctx: Context) -> Optional[Dict[str, Any]]:
    """Get a specific user by their ID.
    
    Args:
        user_id: The unique identifier (ID) of the user.
        ctx: Context object
        
    Returns:
        A dictionary containing the user's details if found, otherwise None.
    """
    await ctx.info(f"Fetching user with ID: {user_id}...")
    
    try:
        result = await users.get_user_by_id(graph_client, user_id)
        await ctx.report_progress(progress=100, total=100)
        if not result:
            await ctx.warning(f"User with ID {user_id} not found.")
        return result
    except AuthenticationError as e:
        error_msg = f"Authentication error: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Error fetching user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_user_sign_ins(user_id: str, ctx: Context, days: int = 7) -> List[Dict[str, Any]]:
    """Get sign-in logs for a specific user within the last N days.

    Requires AuditLog.Read.All permission.
    
    Args:
        user_id: The unique identifier (ID) of the user.
        ctx: Context object
        days: The number of past days to retrieve logs for (default: 7).
        
    Returns:
        A list of dictionaries, each representing a sign-in log event.
    """
    await ctx.info(f"Fetching sign-in logs for user {user_id} for the last {days} days...")
    
    try:
        logs = await signin_logs.get_user_sign_in_logs(graph_client, user_id, days)
        await ctx.report_progress(progress=100, total=100)
        if not logs:
            await ctx.info(f"No sign-in logs found for user {user_id} in the last {days} days.")
        return logs
    except AuthenticationError as e:
        error_msg = f"Authentication error: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Error fetching sign-in logs for {user_id}: {str(e)}"
        # Check for permission errors specifically
        if "Authorization_RequestDenied" in str(e):
             error_msg += " (Ensure the application has AuditLog.Read.All permission)"
             await ctx.error(error_msg)
        else:
            await ctx.error(error_msg)
        logger.error(error_msg)
        raise

@mcp.tool()
async def get_user_mfa_status(user_id: str, ctx: Context) -> Optional[Dict[str, Any]]:
    """Get MFA status and methods for a specific user.
    
    Args:
        user_id: The unique identifier of the user.
        ctx: Context object
        
    Returns:
        A dictionary containing MFA status and methods information.
    """
    await ctx.info(f"Fetching MFA status for user {user_id}...")
    
    try:
        result = await mfa.get_mfa_status(graph_client, user_id)
        await ctx.report_progress(progress=100, total=100)
        if not result:
            await ctx.warning(f"No MFA data found for user {user_id}")
        return result
    except AuthenticationError as e:
        error_msg = f"Authentication error: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Error fetching MFA status for {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_group_mfa_status(group_id: str, ctx: Context) -> List[Dict[str, Any]]:
    """Get MFA status for all members of a group.
    
    Args:
        group_id: The unique identifier of the group.
        ctx: Context object
        
    Returns:
        A list of dictionaries containing MFA status for each group member.
    """
    await ctx.info(f"Fetching MFA status for group {group_id}...")
    
    try:
        results = await mfa.get_group_mfa_status(graph_client, group_id)
        await ctx.report_progress(progress=100, total=100)
        if not results:
            await ctx.warning(f"No MFA data found for group {group_id}")
        return results
    except AuthenticationError as e:
        error_msg = f"Authentication error: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Error fetching group MFA status for {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_privileged_users(ctx: Context) -> List[Dict[str, Any]]:
    """Get all users who are members of privileged directory roles."""
    await ctx.info("Fetching privileged users...")
    try:
        privileged_users = await users.get_privileged_users(graph_client)
        await ctx.report_progress(progress=100, total=100)
        return privileged_users
    except Exception as e:
        await ctx.error(f"Error fetching privileged users: {str(e)}")
        raise

@mcp.tool()
async def get_conditional_access_policies(ctx: Context) -> List[Dict[str, Any]]:
    """Get all conditional access policies.
    
    Args:
        ctx: Context object
    
    Returns:
        A list of dictionaries, each representing a conditional access policy.
    """
    await ctx.info("Fetching conditional access policies...")
    try:
        policies = await conditional_access.get_conditional_access_policies(graph_client)
        await ctx.report_progress(progress=100, total=100)
        return policies
    except Exception as e:
        error_msg = f"Error fetching conditional access policies: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_conditional_access_policy_by_id(policy_id: str, ctx: Context) -> Dict[str, Any]:
    """Get a single conditional access policy by its ID with comprehensive details.
    
    Args:
        policy_id: The unique identifier (ID) of the conditional access policy.
        ctx: Context object
    
    Returns:
        A dictionary containing the policy's details if found, otherwise an empty dict.
    """
    await ctx.info(f"Fetching conditional access policy with ID: {policy_id}...")
    try:
        result = await conditional_access.get_conditional_access_policy_by_id(graph_client, policy_id)
        await ctx.report_progress(progress=100, total=100)
        if not result:
            await ctx.warning(f"Policy with ID {policy_id} not found.")
        return result
    except Exception as e:
        error_msg = f"Error fetching conditional access policy {policy_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_all_groups(ctx: Context, limit: int = 100) -> List[Dict[str, Any]]:
    """Get all groups (up to the specified limit, with paging)."""
    await ctx.info(f"Fetching up to {limit} groups...")
    try:
        results = await groups.get_all_groups(graph_client, limit)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error fetching all groups: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_group_by_id(group_id: str, ctx: Context) -> Optional[Dict[str, Any]]:
    """Get a specific group by its ID."""
    await ctx.info(f"Fetching group with ID: {group_id}...")
    try:
        result = await groups.get_group_by_id(graph_client, group_id)
        await ctx.report_progress(progress=100, total=100)
        if not result:
            await ctx.warning(f"Group with ID {group_id} not found.")
        return result
    except Exception as e:
        error_msg = f"Error fetching group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def search_groups_by_name(name: str, ctx: Context, limit: int = 50) -> List[Dict[str, Any]]:
    """Search for groups by display name (case-insensitive, partial match, with paging)."""
    await ctx.info(f"Searching for groups with name matching '{name}'...")
    try:
        results = await groups.search_groups_by_name(graph_client, name, limit)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error searching groups by name: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_group_members(group_id: str, ctx: Context, limit: int = 100) -> List[Dict[str, Any]]:
    """Get members of a group by group ID (up to the specified limit, with paging)."""
    await ctx.info(f"Fetching up to {limit} members for group {group_id}...")
    try:
        results = await groups.get_group_members(graph_client, group_id, limit)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error fetching group members for group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_user_groups(user_id: str, ctx: Context) -> List[Dict[str, Any]]:
    """Get all groups (including transitive memberships) for a user by user ID."""
    await ctx.info(f"Fetching all groups for user {user_id}...")
    try:
        results = await users.get_user_groups(graph_client, user_id)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error fetching groups for user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_user_roles(user_id: str, ctx: Context) -> List[Dict[str, Any]]:
    """Get all directory roles assigned to a user by user ID."""
    await ctx.info(f"Fetching all directory roles for user {user_id}...")
    try:
        results = await users.get_user_roles(graph_client, user_id)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error fetching roles for user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_all_managed_devices(ctx: Context, filter_os: str = None) -> List[Dict[str, Any]]:
    """Get all managed devices (optionally filter by OS)."""
    await ctx.info(f"Fetching all managed devices{f' with OS {filter_os}' if filter_os else ''}...")
    try:
        results = await managed_devices.get_all_managed_devices(graph_client, filter_os)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error fetching all managed devices: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_managed_devices_by_user(user_id: str, ctx: Context) -> List[Dict[str, Any]]:
    """Get all managed devices for a specific userId."""
    await ctx.info(f"Fetching managed devices for user {user_id}...")
    try:
        results = await managed_devices.get_managed_devices_by_user(graph_client, user_id)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error fetching managed devices for user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_user_audit_logs(user_id: str, ctx: Context, days: int = 30) -> List[Dict[str, Any]]:
    """Get all relevant directory audit logs for a user by user_id within the last N days (default 30)."""
    await ctx.info(f"Fetching directory audit logs for user {user_id} for the last {days} days...")
    try:
        results = await audit_logs.get_user_audit_logs(graph_client, user_id, days)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error fetching directory audit logs for user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def list_user_password_methods(user_id: str, ctx: Context) -> List[Dict[str, Any]]:
    """List a user's password authentication methods."""
    await ctx.info(f"Fetching password authentication methods for user {user_id}...")
    try:
        results = await password_auth.list_user_password_methods(graph_client, user_id)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error listing password methods for user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_user_password_method(user_id: str, method_id: str, ctx: Context) -> Optional[Dict[str, Any]]:
    """Get a specific password authentication method for a user."""
    await ctx.info(f"Fetching password method {method_id} for user {user_id}...")
    try:
        result = await password_auth.get_user_password_method(graph_client, user_id, method_id)
        await ctx.report_progress(progress=100, total=100)
        if not result:
            await ctx.warning(f"Password method {method_id} not found for user {user_id}")
        return result
    except Exception as e:
        error_msg = f"Error getting password method {method_id} for user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def reset_user_password_direct(user_id: str, ctx: Context, password: str = None, require_change_on_next_sign_in: bool = True, generate_password: bool = False, password_length: int = 12) -> Dict[str, Any]:
    """Reset a user's password with a specific password value.
    
    Args:
        user_id: The unique identifier of the user
        ctx: Context object
        password: The new password to set for the user (if None and generate_password is True, a random password will be generated)
        require_change_on_next_sign_in: Whether to require the user to change password on next sign-in (default: True)
        generate_password: Whether to generate a random secure password (default: False)
        password_length: Length of the generated password if generate_password is True (default: 12)
        
    Returns:
        A dictionary with the operation result
    """
    await ctx.info(f"Directly resetting password for user {user_id}...")
    
    try:
        # Generate a secure password if requested
        if generate_password:
            password = generate_secure_password(password_length)
            await ctx.info(f"Generated a secure password of length {password_length}")
        
        # Ensure we have a password
        if not password:
            raise ValueError("Password must be provided or generate_password must be set to True")
        
        result = await password_auth.reset_user_password_direct(graph_client, user_id, password, require_change_on_next_sign_in)
        
        # Include the generated password in the result if we generated one
        if generate_password:
            result['generated_password'] = password
            
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Password successfully reset for user {user_id} using the direct method")
        return result
    except Exception as e:
        error_msg = f"Error directly resetting password for user {user_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def suggest_permissions_for_task(task_category: str, task_name: str, ctx: Context) -> Dict[str, Any]:
    """Suggest Microsoft Graph permissions for a specific task based on common mappings."""
    await ctx.info(f"Suggesting permissions for task '{task_category}/{task_name}'...")
    try:
        result = await permissions_helper.suggest_permissions_for_task(task_category, task_name)
        await ctx.report_progress(progress=100, total=100)
        return result
    except Exception as e:
        error_msg = f"Error suggesting permissions for task {task_category}/{task_name}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def list_permission_categories_and_tasks(ctx: Context) -> Dict[str, Any]:
    """List all available categories and tasks for permission suggestions."""
    await ctx.info("Listing available permission categories and tasks...")
    try:
        result = await permissions_helper.list_available_categories_and_tasks()
        await ctx.report_progress(progress=100, total=100)
        return result
    except Exception as e:
        error_msg = f"Error listing permission categories and tasks: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_all_graph_permissions(ctx: Context) -> Dict[str, Any]:
    """Get all Microsoft Graph permissions directly from the Microsoft Graph API."""
    await ctx.info("Retrieving all Microsoft Graph permissions...")
    try:
        result = await permissions_helper.get_all_graph_permissions(graph_client)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Retrieved {len(result.get('delegated_permissions', []))} delegated and {len(result.get('application_permissions', []))} application permissions")
        return result
    except Exception as e:
        error_msg = f"Error retrieving Microsoft Graph permissions: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def search_permissions(search_term: str, ctx: Context, permission_type: str = None) -> Dict[str, Any]:
    """Search for Microsoft Graph permissions by keyword."""
    await ctx.info(f"Searching for permissions with term '{search_term}'...")
    try:
        result = await permissions_helper.search_permissions(graph_client, search_term, permission_type)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Found {result.get('total_matches', 0)} matching permissions")
        return result
    except Exception as e:
        error_msg = f"Error searching for permissions with term '{search_term}': {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def validate_app_permissions(ctx: Context, client_id: str = "") -> Dict[str, Any]:
    """Validate that the connected app (this server's service principal) has the Microsoft
    Graph application permissions required for EntraID MCP operations.

    Reads the app's actual granted app-role assignments to the Microsoft Graph service
    principal and compares them against the required set. Use this to confirm a tenant
    connection is correctly consented before relying on EntraID tools.

    Args:
        ctx: Context object
        client_id: The app (client) id to check. Defaults to this server's CLIENT_ID env.
    """
    import os
    cid = (client_id or os.environ.get("CLIENT_ID", "")).strip()
    await ctx.info(f"Validating Microsoft Graph permissions for app {cid or '(none)'}...")
    if not cid:
        return {"status": "error", "message": "No client_id provided and CLIENT_ID is not set."}
    try:
        result = await permissions_helper.validate_app_permissions(graph_client, cid)
        await ctx.report_progress(progress=100, total=100)
        return result
    except Exception as e:
        error_msg = f"Error validating app permissions: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        return {"status": "error", "message": str(e)[:500]}

@mcp.tool()
async def create_group(ctx: Context, group_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new group in Microsoft Graph.
    
    Args:
        ctx: Context object
        group_data: Dictionary containing group properties:
          - displayName: Display name of the group (required)
          - mailNickname: Mail alias for the group (required)
          - description: Description of the group (optional)
          - groupTypes: Array of group types e.g. ["Unified"] (optional)
          - mailEnabled: Whether the group is mail-enabled (optional)
          - securityEnabled: Whether the group is a security group (optional)
          - visibility: "Private" or "Public" for Microsoft 365 groups (optional)
          - owners: List of user IDs to add as owners (optional)
          - members: List of IDs to add as members (optional)
          - membershipRule: Rule for dynamic groups (required if DynamicMembership is in groupTypes)
          - membershipRuleProcessingState: "On" or "Paused" for dynamic groups (default: "On")
        
    Returns:
        The created group data with status field if group already exists
    """
    await ctx.info(f"Creating group '{group_data.get('displayName', 'unnamed')}'...")
    
    try:
        # Validate required fields
        if not group_data.get('displayName'):
            raise ValueError("displayName is required for creating a group")
            
        if not group_data.get('mailNickname'):
            raise ValueError("mailNickname is required for creating a group")
        
        # Check if this is a dynamic membership group
        group_types = group_data.get('groupTypes', [])
        is_dynamic = 'DynamicMembership' in group_types
        
        # Validate dynamic group requirements
        if is_dynamic:
            if not group_data.get('membershipRule'):
                raise ValueError("membershipRule is required for dynamic membership groups")
            
            await ctx.info("Creating dynamic membership group with rule: " + group_data.get('membershipRule', ''))
            
        result = await groups.create_group(graph_client, group_data)
        await ctx.report_progress(progress=100, total=100)
        
        # Check if the group already existed
        if result.get('status') == 'already_exists':
            await ctx.info(f"Group with display name '{result.get('displayName')}' already exists (ID: {result.get('id')})")
        else:
            await ctx.info(f"Successfully created group with ID: {result.get('id')}")
            
            # For dynamic groups, inform about membership management
            if is_dynamic:
                await ctx.info("Created dynamic membership group. Members are managed automatically based on the membership rule.")
                
        return result
    except Exception as e:
        error_msg = f"Error creating group: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def update_group(group_id: str, ctx: Context, group_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing group in Microsoft Graph.
    
    Args:
        group_id: ID of the group to update
        ctx: Context object
        group_data: Dictionary containing group properties to update:
          - displayName: Display name of the group (optional)
          - mailNickname: Mail alias for the group (optional)
          - description: Description of the group (optional)
          - visibility: "Private" or "Public" for Microsoft 365 groups (optional)
        
    Returns:
        The updated group data
    """
    await ctx.info(f"Updating group {group_id}...")
    
    try:
        result = await groups.update_group(graph_client, group_id, group_data)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully updated group {group_id}")
        return result
    except Exception as e:
        error_msg = f"Error updating group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def delete_group(group_id: str, ctx: Context) -> Dict[str, Any]:
    """Delete a group from Microsoft Graph.
    
    Args:
        group_id: ID of the group to delete
        ctx: Context object
        
    Returns:
        A dictionary with the operation result
    """
    await ctx.info(f"Deleting group {group_id}...")
    
    try:
        await groups.delete_group(graph_client, group_id)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully deleted group {group_id}")
        return {"status": "success", "message": f"Group {group_id} was deleted successfully"}
    except Exception as e:
        error_msg = f"Error deleting group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def add_group_member(group_id: str, member_id: str, ctx: Context) -> Dict[str, Any]:
    """Add a member to a group.
    
    Args:
        group_id: ID of the group
        member_id: ID of the member (user, group, device, etc.) to add
        ctx: Context object
        
    Returns:
        A dictionary with the operation result
    """
    await ctx.info(f"Adding member {member_id} to group {group_id}...")
    
    try:
        # Try to get the group first to verify if it's a dynamic group
        group = await groups.get_group_by_id(graph_client, group_id)
        if not group:
            raise ValueError(f"Group with ID {group_id} not found")
            
        # Check if this is a dynamic membership group
        if group.get('groupTypes') and 'DynamicMembership' in group.get('groupTypes'):
            error_msg = "Cannot add members to a dynamic membership group. Members are determined by the membership rule."
            await ctx.warning(error_msg)
            return {
                "status": "error", 
                "message": error_msg,
                "groupId": group_id,
                "memberId": member_id,
                "isDynamicGroup": True
            }
        
        # Try to add the member
        await groups.add_group_member(graph_client, group_id, member_id)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully added member {member_id} to group {group_id}")
        return {"status": "success", "message": f"Member {member_id} was added to group {group_id}"}
    except ValueError as e:
        # Handle case where member is already in group
        if "already in group" in str(e).lower():
            message = f"Member {member_id} is already in group {group_id}"
            await ctx.info(message)
            return {"status": "already_exists", "message": message}
        # Otherwise re-raise
        logger.error(f"Value error adding member {member_id} to group {group_id}: {str(e)}")
        await ctx.error(str(e))
        raise
    except Exception as e:
        error_msg = f"Error adding member {member_id} to group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def remove_group_member(group_id: str, member_id: str, ctx: Context) -> Dict[str, Any]:
    """Remove a member from a group.
    
    Args:
        group_id: ID of the group
        member_id: ID of the member to remove
        ctx: Context object
        
    Returns:
        A dictionary with the operation result
    """
    await ctx.info(f"Removing member {member_id} from group {group_id}...")
    
    try:
        # Try to get the group first to verify if it's a dynamic group
        group = await groups.get_group_by_id(graph_client, group_id)
        if not group:
            raise ValueError(f"Group with ID {group_id} not found")
            
        # Check if this is a dynamic membership group
        if group.get('groupTypes') and 'DynamicMembership' in group.get('groupTypes'):
            error_msg = "Cannot remove members from a dynamic membership group. Members are determined by the membership rule."
            await ctx.warning(error_msg)
            return {
                "status": "error", 
                "message": error_msg,
                "groupId": group_id,
                "memberId": member_id,
                "isDynamicGroup": True
            }
            
        # Try to remove the member
        result = await groups.remove_group_member(graph_client, group_id, member_id)
        await ctx.report_progress(progress=100, total=100)
        
        # If we reach here, it was successful (either removed or wasn't a member)
        await ctx.info(f"Successfully removed member {member_id} from group {group_id}")
        return {"status": "success", "message": f"Member {member_id} was removed from group {group_id}"}
    except ValueError as e:
        # Handle case where member is not in group
        if "not found in group" in str(e).lower():
            message = f"Member {member_id} is not in group {group_id}"
            await ctx.info(message)
            return {"status": "not_found", "message": message}
        # Otherwise re-raise
        logger.error(f"Value error removing member {member_id} from group {group_id}: {str(e)}")
        await ctx.error(str(e))
        raise
    except Exception as e:
        error_msg = f"Error removing member {member_id} from group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def add_group_owner(group_id: str, owner_id: str, ctx: Context) -> Dict[str, Any]:
    """Add an owner to a group.
    
    Args:
        group_id: ID of the group
        owner_id: ID of the user to add as owner
        ctx: Context object
        
    Returns:
        A dictionary with the operation result
    """
    await ctx.info(f"Adding owner {owner_id} to group {group_id}...")
    
    try:
        await groups.add_group_owner(graph_client, group_id, owner_id)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully added owner {owner_id} to group {group_id}")
        return {"status": "success", "message": f"Owner {owner_id} was added to group {group_id}"}
    except Exception as e:
        error_msg = f"Error adding owner {owner_id} to group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def remove_group_owner(group_id: str, owner_id: str, ctx: Context) -> Dict[str, Any]:
    """Remove an owner from a group.
    
    Args:
        group_id: ID of the group
        owner_id: ID of the owner to remove
        ctx: Context object
        
    Returns:
        A dictionary with the operation result
    """
    await ctx.info(f"Removing owner {owner_id} from group {group_id}...")
    
    try:
        await groups.remove_group_owner(graph_client, group_id, owner_id)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully removed owner {owner_id} from group {group_id}")
        return {"status": "success", "message": f"Owner {owner_id} was removed from group {group_id}"}
    except Exception as e:
        error_msg = f"Error removing owner {owner_id} from group {group_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def list_applications(ctx: Context, limit: int = 100) -> List[Dict[str, Any]]:
    """List all applications (app registrations) in the tenant, with paging."""
    await ctx.info(f"Listing up to {limit} applications...")
    try:
        results = await applications.list_applications(graph_client, limit)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error listing applications: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_application_by_id(app_id: str, ctx: Context) -> Optional[Dict[str, Any]]:
    """Get a specific application by its object ID."""
    await ctx.info(f"Fetching application with ID: {app_id}...")
    try:
        result = await applications.get_application_by_id(graph_client, app_id)
        await ctx.report_progress(progress=100, total=100)
        if not result:
            await ctx.warning(f"Application with ID {app_id} not found.")
        return result
    except Exception as e:
        error_msg = f"Error fetching application {app_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def create_application(ctx: Context, app_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new application (app registration)."""
    await ctx.info(f"Creating application '{app_data.get('displayName', 'unnamed')}'...")
    try:
        result = await applications.create_application(graph_client, app_data)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully created application with ID: {result.get('id')}")
        return result
    except Exception as e:
        error_msg = f"Error creating application: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def update_application(app_id: str, ctx: Context, app_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing application (app registration)."""
    await ctx.info(f"Updating application {app_id}...")
    try:
        result = await applications.update_application(graph_client, app_id, app_data)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully updated application {app_id}")
        return result
    except Exception as e:
        error_msg = f"Error updating application {app_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def delete_application(app_id: str, ctx: Context) -> Dict[str, Any]:
    """Delete an application (app registration) by its object ID."""
    await ctx.info(f"Deleting application {app_id}...")
    try:
        await applications.delete_application(graph_client, app_id)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully deleted application {app_id}")
        return {"status": "success", "message": f"Application {app_id} was deleted successfully"}
    except Exception as e:
        error_msg = f"Error deleting application {app_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def list_service_principals(ctx: Context, limit: int = 100) -> List[Dict[str, Any]]:
    """List all service principals in the tenant, with paging."""
    await ctx.info(f"Listing up to {limit} service principals...")
    try:
        results = await service_principals.list_service_principals(graph_client, limit)
        await ctx.report_progress(progress=100, total=100)
        return results
    except Exception as e:
        error_msg = f"Error listing service principals: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def get_service_principal_by_id(sp_id: str, ctx: Context) -> Optional[Dict[str, Any]]:
    """Get a specific service principal by its object ID."""
    await ctx.info(f"Fetching service principal with ID: {sp_id}...")
    try:
        result = await service_principals.get_service_principal_by_id(graph_client, sp_id)
        await ctx.report_progress(progress=100, total=100)
        if not result:
            await ctx.warning(f"Service principal with ID {sp_id} not found.")
        return result
    except Exception as e:
        error_msg = f"Error fetching service principal {sp_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def create_service_principal(ctx: Context, sp_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new service principal."""
    await ctx.info(f"Creating service principal for appId '{sp_data.get('appId', 'unknown')}'...")
    try:
        result = await service_principals.create_service_principal(graph_client, sp_data)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully created service principal with ID: {result.get('id')}")
        return result
    except Exception as e:
        error_msg = f"Error creating service principal: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def update_service_principal(sp_id: str, ctx: Context, sp_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing service principal."""
    await ctx.info(f"Updating service principal {sp_id}...")
    try:
        result = await service_principals.update_service_principal(graph_client, sp_id, sp_data)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully updated service principal {sp_id}")
        return result
    except Exception as e:
        error_msg = f"Error updating service principal {sp_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

@mcp.tool()
async def delete_service_principal(sp_id: str, ctx: Context) -> Dict[str, Any]:
    """Delete a service principal by its object ID."""
    await ctx.info(f"Deleting service principal {sp_id}...")
    try:
        await service_principals.delete_service_principal(graph_client, sp_id)
        await ctx.report_progress(progress=100, total=100)
        await ctx.info(f"Successfully deleted service principal {sp_id}")
        return {"status": "success", "message": f"Service principal {sp_id} was deleted successfully"}
    except Exception as e:
        error_msg = f"Error deleting service principal {sp_id}: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise

# Add a dynamic greeting resource
@mcp.resource("greeting://{name}")
def get_greeting(name: str) -> str:
    """Get a personalized greeting"""
    return f"Hello, {name}!"


@mcp.tool()
async def find_expiring_credentials(
    ctx: Context,
    within_days: int = 30,
    include_expired: bool = True,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Find applications (app registrations) and service principals whose client
    secrets or certificates are expired or nearing expiry.

    Use this to answer questions like "find service principals or app secrets nearing
    expiry". Returns one entry per credential, sorted soonest-to-expire first, with the
    owner (application / servicePrincipal), display name, credential type
    (secret/certificate), expiry date, and days until expiry (negative = already
    expired). Requires Application.Read.All / Directory.Read.All.

    Args:
        ctx: Context object
        within_days: Include credentials expiring within this many days (default 30).
        include_expired: Also include already-expired credentials (default True).
        limit: Max applications and service principals to scan (default 200).
    """
    await ctx.info(f"Scanning app & service-principal credentials expiring within {within_days} days...")
    try:
        results = await applications.find_expiring_credentials(
            graph_client, within_days=within_days, include_expired=include_expired, limit=limit
        )
        await ctx.report_progress(progress=100, total=100)
        if not results:
            await ctx.info("No expiring or expired credentials found.")
        return results
    except AuthenticationError as e:
        error_msg = f"Authentication error: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Error finding expiring credentials: {str(e)}"
        if "Authorization_RequestDenied" in str(e):
            error_msg += " (Ensure the app has Application.Read.All / Directory.Read.All)"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise


@mcp.tool()
async def find_ownerless_applications(ctx: Context, limit: int = 200) -> List[Dict[str, Any]]:
    """Find application registrations that have NO assigned owners.

    Ownerless app registrations are a governance risk — nobody is accountable for
    rotating their secrets/certificates or decommissioning them, a recurring cause of
    identity incidents. Returns one entry per ownerless application with its object id,
    appId, display name, creation date and sign-in audience. Requires
    Application.Read.All / Directory.Read.All.

    Args:
        ctx: Context object
        limit: Max applications to scan (default 200).
    """
    await ctx.info(f"Scanning up to {limit} app registrations for missing owners...")
    try:
        results = await applications.find_ownerless_applications(graph_client, limit=limit)
        await ctx.report_progress(progress=100, total=100)
        if not results:
            await ctx.info("No ownerless application registrations found.")
        return results
    except AuthenticationError as e:
        error_msg = f"Authentication error: {str(e)}"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Error finding ownerless applications: {str(e)}"
        if "Authorization_RequestDenied" in str(e):
            error_msg += " (Ensure the app has Application.Read.All / Directory.Read.All)"
        logger.error(error_msg)
        await ctx.error(error_msg)
        raise
