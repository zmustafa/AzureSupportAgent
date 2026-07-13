import logging
from typing import Dict, List, Any, Optional
from kiota_abstractions.base_request_configuration import RequestConfiguration
from utils.graph_client import GraphClient
from msgraph.generated.models.user import User
from msgraph.generated.models.password_profile import PasswordProfile


logger = logging.getLogger(__name__)

async def list_user_password_methods(graph_client: GraphClient, user_id: str) -> List[Dict[str, Any]]:
    """List a user's password authentication methods.
    
    Args:
        graph_client: GraphClient instance
        user_id: The unique identifier of the user
        
    Returns:
        A list of password authentication methods
    """
    try:
        client = graph_client.get_client()
        response = await client.users.by_user_id(user_id).authentication.password_methods.get()
        
        formatted_methods = []
        if response and response.value:
            for method in response.value:
                method_data = {
                    'id': getattr(method, 'id', None),
                    'createdDateTime': method.created_date_time.isoformat() if getattr(method, 'created_date_time', None) else None
                }
                formatted_methods.append(method_data)
                
        return formatted_methods
    except Exception as e:
        logger.error(f"Error listing password methods for user {user_id}: {str(e)}")
        raise

async def get_user_password_method(graph_client: GraphClient, user_id: str, method_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific password authentication method for a user.
    
    Args:
        graph_client: GraphClient instance
        user_id: The unique identifier of the user
        method_id: The identifier of the password method
        
    Returns:
        A password authentication method or None if not found
    """
    try:
        client = graph_client.get_client()
        method = await client.users.by_user_id(user_id).authentication.password_methods.by_password_authentication_method_id(method_id).get()
        
        if method:
            method_data = {
                'id': getattr(method, 'id', None),
                'createdDateTime': method.created_date_time.isoformat() if getattr(method, 'created_date_time', None) else None
            }
            return method_data
        return None
    except Exception as e:
        logger.error(f"Error getting password method {method_id} for user {user_id}: {str(e)}")
        raise


async def reset_user_password_direct(graph_client: GraphClient, user_id: str, password: str, require_change_on_next_sign_in: bool = True) -> Dict[str, Any]:
    """Reset a user's password by directly updating the user object with a specific password.
    
    Args:
        graph_client: GraphClient instance
        user_id: The unique identifier of the user
        password: The new password to set for the user
        require_change_on_next_sign_in: Whether to require the user to change their password on next sign-in, default is True
        
    Returns:
        A dictionary with the operation result
    """
    try:
        client = graph_client.get_client()
        
        # Create a password profile with the provided password
        password_profile = PasswordProfile(
            force_change_password_next_sign_in=require_change_on_next_sign_in,
            password=password
        )
        
        # Create the request body
        request_body = User(
            password_profile=password_profile
        )
        
        # Update the user
        await client.users.by_user_id(user_id).patch(request_body)
        
        # Return success result (Note: For security, we don't return the actual password)
        return {
            'status': 'success',
            'userId': user_id,
            'passwordResetRequired': require_change_on_next_sign_in,
            'message': 'Password has been reset using the direct method.'
        }
    except Exception as e:
        logger.error(f"Error directly resetting password for user {user_id}: {str(e)}")
        raise 