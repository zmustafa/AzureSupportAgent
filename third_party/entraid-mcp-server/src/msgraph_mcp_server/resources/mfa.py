"""MFA resource module for Microsoft Graph.

This module provides access to Microsoft Graph MFA-related resources.
"""

import logging
from typing import Dict, List, Optional, Any

from msgraph.generated.users.item.user_item_request_builder import UserItemRequestBuilder
from kiota_abstractions.base_request_configuration import RequestConfiguration

from utils.graph_client import GraphClient

logger = logging.getLogger(__name__)

async def get_mfa_status(graph_client: GraphClient, user_id: str) -> Dict[str, Any]:
    """Get MFA status and methods for a specific user.
    
    Args:
        graph_client: GraphClient instance
        user_id: The unique identifier of the user.
        
    Returns:
        A dictionary containing MFA status and methods information.
    """
    try:
        client = graph_client.get_client()
        
        # Get user's company name
        query_params = UserItemRequestBuilder.UserItemRequestBuilderGetQueryParameters(
            select=["companyName"]
        )
        request_configuration = RequestConfiguration(
            query_parameters=query_params
        )
        user = await client.users.by_user_id(user_id).get(request_configuration=request_configuration)
        
        # Get MFA methods
        mfa_data = await client.users.by_user_id(user_id).authentication.methods.get()
        
        if not mfa_data:
            logger.warning(f"No MFA data found for user {user_id}")
            return None
            
        # Initialize MFA status object
        mfa_status = {
            'userPrincipalName': user_id,
            'mail': user.mail if user else None,
            'companyName': user.company_name if user else None,
            'mfaStatus': 'Disabled',
            'methods': {
                'email': False,
                'fido2': False,
                'authenticatorApp': False,
                'password': False,
                'phone': False,
                'softwareOath': False,
                'temporaryAccessPass': False,
                'windowsHelloForBusiness': False
            }
        }
        
        # Process each authentication method
        for method in mfa_data.value:
            method_type = method.odata_type
            
            if method_type == "#microsoft.graph.emailAuthenticationMethod":
                mfa_status['methods']['email'] = True
                mfa_status['mfaStatus'] = "Enabled"
            elif method_type == "#microsoft.graph.fido2AuthenticationMethod":
                mfa_status['methods']['fido2'] = True
                mfa_status['mfaStatus'] = "Enabled"
            elif method_type == "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod":
                mfa_status['methods']['authenticatorApp'] = True
                mfa_status['mfaStatus'] = "Enabled"
            elif method_type == "#microsoft.graph.passwordAuthenticationMethod":
                mfa_status['methods']['password'] = True
                if mfa_status['mfaStatus'] != "Enabled":
                    mfa_status['mfaStatus'] = "Disabled"
            elif method_type == "#microsoft.graph.phoneAuthenticationMethod":
                mfa_status['methods']['phone'] = True
                mfa_status['mfaStatus'] = "Enabled"
            elif method_type == "#microsoft.graph.softwareOathAuthenticationMethod":
                mfa_status['methods']['softwareOath'] = True
                mfa_status['mfaStatus'] = "Enabled"
            elif method_type == "#microsoft.graph.temporaryAccessPassAuthenticationMethod":
                mfa_status['methods']['temporaryAccessPass'] = True
                mfa_status['mfaStatus'] = "Enabled"
            elif method_type == "#microsoft.graph.windowsHelloForBusinessAuthenticationMethod":
                mfa_status['methods']['windowsHelloForBusiness'] = True
                mfa_status['mfaStatus'] = "Enabled"
        
        return mfa_status
        
    except Exception as e:
        logger.error(f"Error fetching MFA status for user {user_id}: {str(e)}")
        raise

async def get_group_mfa_status(graph_client: GraphClient, group_id: str) -> List[Dict[str, Any]]:
    """Get MFA status for all members of a group.
    
    Args:
        graph_client: GraphClient instance
        group_id: The unique identifier of the group.
        
    Returns:
        A list of dictionaries containing MFA status for each group member.
    """
    try:
        client = graph_client.get_client()
        
        # Get group members
        members = await client.groups.by_group_id(group_id).members.get()
        if not members or not members.value:
            logger.warning(f"No members found in group {group_id}")
            return []
            
        # Process each member's MFA status
        mfa_statuses = []
        for member in members.value:
            try:
                mfa_status = await get_mfa_status(graph_client, member.id)
                if mfa_status:
                    mfa_statuses.append(mfa_status)
            except Exception as e:
                logger.error(f"Error processing member {member.id}: {str(e)}")
                continue
                
        return mfa_statuses
        
    except Exception as e:
        logger.error(f"Error fetching group MFA status for group {group_id}: {str(e)}")
        raise 