"""The resources package for Entra ID MCP Server.

This package contains modules for interacting with Microsoft Graph resources.
"""

# Import modules to make them available through resources package
from . import users, groups, signin_logs, mfa, conditional_access, managed_devices, audit_logs, password_auth, permissions_helper
from . import applications, service_principals