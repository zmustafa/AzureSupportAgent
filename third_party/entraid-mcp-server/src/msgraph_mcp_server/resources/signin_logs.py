"""Sign-in logs resource module for Microsoft Graph.

This module provides access to Microsoft Graph sign-in logs.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone

from msgraph.generated.audit_logs.sign_ins.sign_ins_request_builder import SignInsRequestBuilder
from kiota_abstractions.base_request_configuration import RequestConfiguration

from utils.graph_client import GraphClient

logger = logging.getLogger(__name__)

async def get_user_sign_in_logs(graph_client: GraphClient, user_id: str, days: int = 7) -> List[Dict[str, Any]]:
    """Get sign-in logs for a specific user within the last N days.
    
    Args:
        graph_client: GraphClient instance
        user_id: The unique identifier of the user.
        days: The number of past days to retrieve logs for (default: 7).
        
    Returns:
        A list of dictionaries, each representing a sign-in log event.
    """
    try:
        client = graph_client.get_client()
        
        # Calculate date range
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)
        
        # Format dates for query using the exact format from documentation
        start_date_str = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date_str = end_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # Define the OData filter query with proper formatting
        filter_query = f"createdDateTime ge {start_date_str} and createdDateTime le {end_date_str} and userId eq '{user_id}'"
        
        logger.info(f"Fetching sign-in logs for user ID: {user_id}")
        logger.info(f"Date range: {start_date_str} to {end_date_str}")
        logger.info(f"Filter query: {filter_query}")
        
        # Set up query parameters using SignInsRequestBuilder
        query_params = SignInsRequestBuilder.SignInsRequestBuilderGetQueryParameters(
            filter=filter_query,
            orderby=['createdDateTime desc'],
            top=1000  # Increased from default to get more logs
        )
        
        # Create request configuration
        request_configuration = RequestConfiguration(
            query_parameters=query_params
        )
        request_configuration.headers.add("ConsistencyLevel", "eventual")
        
        # Execute the request
        sign_ins = await client.audit_logs.sign_ins.get(request_configuration=request_configuration)

        formatted_logs = []
        if sign_ins and sign_ins.value:
            logger.info(f"Found {len(sign_ins.value)} sign-in records")
            
            for log in sign_ins.value:
                # Format each log entry with comprehensive fields
                log_data = {
                    "id": log.id,
                    "createdDateTime": log.created_date_time.isoformat() if log.created_date_time else None,
                    "userId": log.user_id,
                    "userDisplayName": log.user_display_name,
                    "userPrincipalName": log.user_principal_name,
                    "appDisplayName": log.app_display_name,
                    "appId": log.app_id,
                    "ipAddress": log.ip_address,
                    "clientAppUsed": log.client_app_used,
                    "correlationId": log.correlation_id,
                    "isInteractive": log.is_interactive,
                    "resourceDisplayName": log.resource_display_name,
                    "status": {
                        "errorCode": log.status.error_code if log.status else None,
                        "failureReason": log.status.failure_reason if log.status else None,
                        "additionalDetails": log.status.additional_details if log.status else None
                    },
                    "riskInformation": {
                        "riskDetail": log.risk_detail,
                        "riskLevelAggregated": log.risk_level_aggregated,
                        "riskLevelDuringSignIn": log.risk_level_during_sign_in,
                        "riskState": log.risk_state,
                        "riskEventTypes": log.risk_event_types_v2 if hasattr(log, 'risk_event_types_v2') else []
                    }
                }
                
                # Add device details if available
                if hasattr(log, 'device_detail') and log.device_detail:
                    device = log.device_detail
                    log_data["deviceDetail"] = {
                        "deviceId": device.device_id,
                        "displayName": device.display_name,
                        "operatingSystem": device.operating_system,
                        "browser": device.browser,
                        "isCompliant": device.is_compliant,
                        "isManaged": device.is_managed,
                        "trustType": device.trust_type
                    }
                
                # Add location if available
                if hasattr(log, 'location') and log.location:
                    location = log.location
                    log_data["location"] = {
                        "city": location.city,
                        "state": location.state,
                        "countryOrRegion": location.country_or_region,
                        "coordinates": None
                    }
                    
                    # Add coordinates if available
                    if hasattr(location, 'geo_coordinates') and location.geo_coordinates:
                        log_data["location"]["coordinates"] = {
                            "latitude": location.geo_coordinates.latitude,
                            "longitude": location.geo_coordinates.longitude
                        }
                
                formatted_logs.append(log_data)
        else:
            logger.info(f"No sign-in logs found for user {user_id} in the last {days} days.")
            
        return formatted_logs
        
    except Exception as e:
        logger.error(f"Error fetching sign-in logs for user {user_id}: {str(e)}")
        # Check for permission errors specifically
        if "Authorization_RequestDenied" in str(e):
             logger.error("Permission denied. Ensure the application has AuditLog.Read.All permission.")
        raise 