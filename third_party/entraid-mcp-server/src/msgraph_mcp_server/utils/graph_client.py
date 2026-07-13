"""Microsoft Graph client utility.

This module provides a utility class for making requests to the Microsoft Graph API.
"""

import logging
from typing import Any, Dict, List, Optional

from msgraph import GraphServiceClient

from auth.graph_auth import GraphAuthManager

class GraphClient:
    """Core client utility for Microsoft Graph API interactions.
    
    This class is responsible for:
    1. Initializing and managing the Microsoft Graph client
    2. Providing core API functionality
    3. Handling shared request configurations
    """
    
    def __init__(self, auth_manager: GraphAuthManager):
        """Initialize the GraphClient.
        
        Args:
            auth_manager: GraphAuthManager instance for authentication
        """
        self.auth_manager = auth_manager
        self._client = None
        self.logger = logging.getLogger(__name__)
    
    def get_client(self) -> GraphServiceClient:
        """Get or create a Graph client.
        
        Returns:
            Initialized GraphServiceClient
        """
        if self._client is None:
            self._client = self.auth_manager.get_graph_client()
            self.logger.info("Graph client initialized")
        return self._client
    
    async def execute_request(self, request_func, *args, **kwargs):
        """Execute a Graph API request with proper error handling.
        
        Args:
            request_func: The Graph API request function to call
            *args: Arguments to pass to the request function
            **kwargs: Keyword arguments to pass to the request function
            
        Returns:
            The response from the API
            
        Raises:
            Exception: If the request fails
        """
        try:
            client = self.get_client()
            response = await request_func(*args, **kwargs)
            return response
        except Exception as e:
            self.logger.error(f"Error executing Graph API request: {str(e)}")
            if "Authorization_RequestDenied" in str(e):
                self.logger.error("Permission denied. Check application permissions.")
            raise