"""Microsoft Graph authentication module.

This module provides authentication functionality for the Microsoft Graph API
using Azure Identity credentials.
"""

import os
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
from azure.identity import ClientSecretCredential, CertificateCredential
from msgraph import GraphServiceClient

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Try to load environment variables from multiple possible locations
env_paths = [
    # Project root config directory
    Path(__file__).parent.parent.parent / "config" / ".env",
    # Current working directory config
    Path.cwd() / "config" / ".env",
    # User's home directory
    Path.home() / ".entraid" / ".env",
    # System-wide config
    Path("/etc/entraid/.env")
]

env_loaded = False
for env_path in env_paths:
    if env_path.exists():
        load_dotenv(env_path)
        logger.info(f"Loaded environment variables from {env_path}")
        env_loaded = True
        break

if not env_loaded:
    logger.warning("No .env file found in any of the expected locations")

class AuthenticationError(Exception):
    """Custom exception for authentication errors"""
    pass

class GraphAuthManager:
    """Authentication manager for Microsoft Graph API."""
    
    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        certificate_path: Optional[str] = None,
        certificate_pwd: Optional[str] = None,
        scopes: Optional[List[str]] = None
    ):
        """Initialize the GraphAuthManager.
        
        Args:
            tenant_id: Azure tenant ID
            client_id: Azure application client ID
            client_secret: Azure application client secret
            certificate_path: Path to certificate file
            certificate_pwd: Certificate password
            scopes: List of Microsoft Graph API scopes to request
        """
        # Try to get credentials from parameters first, then environment
        self.tenant_id = tenant_id or os.environ.get("TENANT_ID")
        self.client_id = client_id or os.environ.get("CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("CLIENT_SECRET")
        self.certificate_path = certificate_path or os.environ.get("CERTIFICATE_PATH")
        self.certificate_pwd = certificate_pwd or os.environ.get("CERTIFICATE_PWD")
        self.scopes = scopes or ["https://graph.microsoft.com/.default"]
        self._graph_client = None
        
        # Log the state of credentials (without exposing sensitive data)
        logger.info("Initializing GraphAuthManager with credentials:")
        logger.info(f"TENANT_ID: {'Set' if self.tenant_id else 'Not set'}")
        logger.info(f"CLIENT_ID: {'Set' if self.client_id else 'Not set'}")
        logger.info(f"CLIENT_SECRET: {'Set' if self.client_secret else 'Not set'}")
        
        # Validate credentials
        self._validate_credentials()
    
    def _validate_credentials(self):
        """Validate that all required credentials are present."""
        missing = []
        if not self.tenant_id:
            missing.append("tenant_id")
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
            
        if missing:
            error_msg = f"Missing required credentials: {', '.join(missing)}"
            logger.error(error_msg)
            raise AuthenticationError(error_msg)
    
    def get_auth_method(self) -> str:
        """Determine the authentication method to use."""
        if self.certificate_path and self.certificate_pwd:
            return 'certificate'
        elif self.client_secret:
            return 'client_secret'
        else:
            # Try to determine from environment
            if os.environ.get("CERTIFICATE_PWD"):
                return 'certificate'
            else:
                return 'client_secret'
    
    def get_auth_params(self) -> Dict[str, str]:
        """Get authentication parameters."""
        params = {
            'client_id': self.client_id,
            'tenant_id': self.tenant_id
        }
        
        auth_method = self.get_auth_method()
        if auth_method == 'certificate':
            params['certificate_path'] = self.certificate_path
            params['certificate_pwd'] = self.certificate_pwd
        elif auth_method == 'client_secret':
            params['client_secret'] = self.client_secret
        
        return params
    
    def get_graph_client(self) -> GraphServiceClient:
        """Get a Microsoft Graph client.
        
        Returns:
            GraphServiceClient: Authenticated Microsoft Graph client
            
        Raises:
            AuthenticationError: If authentication fails or required parameters are missing
        """
        if self._graph_client:
            return self._graph_client
            
        try:
            credential = ClientSecretCredential(
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                client_secret=self.client_secret
            )
            
            self._graph_client = GraphServiceClient(
                credentials=credential,
                scopes=self.scopes
            )
            logger.info("Successfully created Graph client")
            return self._graph_client
            
        except Exception as e:
            error_msg = f"Failed to create Graph client: {str(e)}"
            logger.error(error_msg)
            raise AuthenticationError(error_msg)
    
    def get_auth_params_from_env(self) -> Tuple[Dict[str, Any], str]:
        """
        Get authentication parameters from environment variables.
        Supports both local .env and pipeline certificate authentication.
        
        Returns:
            Tuple of (params dict, auth_method)
        """
        params = {}
        
        # Get common parameters
        params['client_id'] = os.environ.get('CLIENT_ID')
        params['tenant_id'] = os.environ.get('TENANT_ID')
        
        # Check for certificate authentication (Pipeline)
        if os.environ.get('CERTIFICATE_PWD'):
            params['certificate_path'] = os.path.join(os.environ.get('AGENT_TEMPDIRECTORY', ''), 
                                                  os.environ.get('CERT_NAME', ''))
            params['certificate_pwd'] = os.environ.get('CERTIFICATE_PWD')
            return params, 'certificate'
        
        # Check for client secret authentication (Local)
        elif os.environ.get('CLIENT_SECRET'):
            params['client_secret'] = os.environ.get('CLIENT_SECRET')
            return params, 'client_secret'
        
        raise AuthenticationError("No valid authentication parameters found in environment")

def get_graph_client(auth_method=None, **kwargs):
    """
    Get a Microsoft Graph client using either certificate or client secret authentication.
    
    Args:
        auth_method (str): Either 'certificate' or 'client_secret'. If None, will try to determine from environment.
        **kwargs: Additional arguments for authentication:
            - For certificate: client_id, tenant_id, certificate_path, certificate_pwd
            - For client secret: client_id, tenant_id, client_secret
    
    Returns:
        GraphServiceClient: Authenticated Microsoft Graph client
    
    Raises:
        AuthenticationError: If authentication fails or required parameters are missing
    """
    try:
        # If auth_method is not specified, try to determine from environment
        if auth_method is None:
            # Check if we're in a pipeline environment (certificate auth)
            if os.environ.get('CERTIFICATE_PWD'):
                auth_method = 'certificate'
            else:
                # Try to load from .env file
                load_dotenv()
                if os.environ.get('CLIENT_SECRET'):
                    auth_method = 'client_secret'
                else:
                    raise AuthenticationError("Could not determine authentication method. Please specify auth_method or set up environment variables.")

        # Set up logging
        logging.info(f"Using authentication method: {auth_method}")

        if auth_method == 'certificate':
            # Certificate authentication (Pipeline)
            required_params = ['client_id', 'tenant_id', 'certificate_path', 'certificate_pwd']
            missing_params = [param for param in required_params if param not in kwargs]
            if missing_params:
                raise AuthenticationError(f"Missing required parameters for certificate authentication: {', '.join(missing_params)}")

            credential = CertificateCredential(
                tenant_id=kwargs['tenant_id'],
                client_id=kwargs['client_id'],
                certificate_path=kwargs['certificate_path'],
                password=kwargs['certificate_pwd'],
                connection_verify=certifi.where()
            )
            logging.info("Using certificate-based authentication")

        elif auth_method == 'client_secret':
            # Client secret authentication (Local development)
            required_params = ['client_id', 'tenant_id', 'client_secret']
            missing_params = [param for param in required_params if param not in kwargs]
            if missing_params:
                raise AuthenticationError(f"Missing required parameters for client secret authentication: {', '.join(missing_params)}")

            credential = ClientSecretCredential(
                tenant_id=kwargs['tenant_id'],
                client_id=kwargs['client_id'],
                client_secret=kwargs['client_secret'],
                connection_verify=certifi.where()
            )
            logging.info("Using client secret-based authentication")

        else:
            raise AuthenticationError(f"Invalid authentication method: {auth_method}")

        # Create and return the Graph client
        scopes = ['https://graph.microsoft.com/.default']
        client = GraphServiceClient(
            credentials=credential,
            scopes=scopes
        )
        logging.info("Successfully created Graph client")
        return client

    except Exception as e:
        logging.error(f"Authentication failed: {str(e)}")
        raise AuthenticationError(f"Failed to authenticate: {str(e)}")

def get_auth_params_from_env():
    """
    Get authentication parameters from environment variables.
    Supports both local .env and pipeline certificate authentication.
    
    Returns:
        dict: Dictionary containing authentication parameters
    """
    params = {}
    
    # Try to load from .env file first
    load_dotenv()
    
    # Get common parameters
    params['client_id'] = os.environ.get('CLIENT_ID')
    params['tenant_id'] = os.environ.get('TENANT_ID')
    
    # Check for certificate authentication (Pipeline)
    if os.environ.get('CERTIFICATE_PWD'):
        params['certificate_path'] = os.path.join(os.environ.get('AGENT_TEMPDIRECTORY', ''), 
                                               os.environ.get('CERT_NAME', ''))
        params['certificate_pwd'] = os.environ.get('CERTIFICATE_PWD')
        return params, 'certificate'
    
    # Check for client secret authentication (Local)
    elif os.environ.get('CLIENT_SECRET'):
        params['client_secret'] = os.environ.get('CLIENT_SECRET')
        return params, 'client_secret'
    
    raise AuthenticationError("No valid authentication parameters found in environment") 