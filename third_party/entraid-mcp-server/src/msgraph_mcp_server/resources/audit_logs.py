import logging
from typing import Dict, List, Any
from datetime import datetime, timedelta, timezone
from msgraph.generated.audit_logs.directory_audits.directory_audits_request_builder import DirectoryAuditsRequestBuilder
from kiota_abstractions.base_request_configuration import RequestConfiguration
from utils.graph_client import GraphClient

logger = logging.getLogger(__name__)

async def get_user_audit_logs(graph_client: GraphClient, user_id: str, days: int = 30) -> List[Dict[str, Any]]:
    """Get all relevant directory audit logs for a user by user_id within the last N days (default 30), with paging support."""
    try:
        client = graph_client.get_client()
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)
        start_date_str = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_date_str = end_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        # Filter: initiatedBy/user/id eq '{user_id}' and activityDateTime in range
        filter_query = f"initiatedBy/user/id eq '{user_id}' and activityDateTime ge {start_date_str} and activityDateTime le {end_date_str}"
        logger.info(f"Fetching directory audit logs for user ID: {user_id}")
        logger.info(f"Date range: {start_date_str} to {end_date_str}")
        logger.info(f"Filter query: {filter_query}")
        query_params = DirectoryAuditsRequestBuilder.DirectoryAuditsRequestBuilderGetQueryParameters(
            filter=filter_query,
            orderby=["activityDateTime desc"],
            top=1000
        )
        request_configuration = RequestConfiguration(query_parameters=query_params)
        request_configuration.headers.add("ConsistencyLevel", "eventual")
        response = await client.audit_logs.directory_audits.get(request_configuration=request_configuration)
        logs = []
        if response and response.value:
            logs.extend(response.value)
        while response is not None and getattr(response, 'odata_next_link', None):
            response = await client.audit_logs.directory_audits.with_url(response.odata_next_link).get(request_configuration=request_configuration)
            if response and response.value:
                logs.extend(response.value)
        formatted_logs = []
        for log in logs:
            log_data = {
                "id": getattr(log, "id", None),
                "activityDateTime": log.activity_date_time.isoformat() if getattr(log, "activity_date_time", None) else None,
                "activityDisplayName": getattr(log, "activity_display_name", None),
                "category": getattr(log, "category", None),
                "operationType": getattr(log, "operation_type", None),
                "result": getattr(log, "result", None),
                "resultReason": getattr(log, "result_reason", None),
                "initiatedBy": None,
                "targetResources": None,
                "loggedByService": getattr(log, "logged_by_service", None),
                "correlationId": getattr(log, "correlation_id", None),
                "additionalDetails": [
                    {"key": getattr(kv, 'key', None), "value": getattr(kv, 'value', None)} for kv in getattr(log, 'additional_details', [])
                ] if hasattr(log, 'additional_details') and log.additional_details else [],
            }
            # initiatedBy
            if hasattr(log, 'initiated_by') and log.initiated_by:
                ib = log.initiated_by
                log_data["initiatedBy"] = {
                    "user": {
                        "id": getattr(ib.user, 'id', None) if hasattr(ib, 'user') and ib.user else None,
                        "displayName": getattr(ib.user, 'display_name', None) if hasattr(ib, 'user') and ib.user else None,
                        "userPrincipalName": getattr(ib.user, 'user_principal_name', None) if hasattr(ib, 'user') and ib.user else None
                    } if hasattr(ib, 'user') and ib.user else None,
                    "app": {
                        "appId": getattr(ib.app, 'app_id', None) if hasattr(ib, 'app') and ib.app else None,
                        "displayName": getattr(ib.app, 'display_name', None) if hasattr(ib, 'app') and ib.app else None
                    } if hasattr(ib, 'app') and ib.app else None
                }
            # targetResources
            if hasattr(log, 'target_resources') and log.target_resources:
                log_data["targetResources"] = [
                    {
                        "id": getattr(tr, 'id', None),
                        "displayName": getattr(tr, 'display_name', None),
                        "type": getattr(tr, 'type', None),
                        "userPrincipalName": getattr(tr, 'user_principal_name', None),
                        "modifiedProperties": [
                            {
                                "displayName": getattr(mp, 'display_name', None),
                                "oldValue": getattr(mp, 'old_value', None),
                                "newValue": getattr(mp, 'new_value', None)
                            } for mp in getattr(tr, 'modified_properties', [])
                        ] if hasattr(tr, 'modified_properties') and tr.modified_properties else []
                    }
                    for tr in log.target_resources
                ]
            formatted_logs.append(log_data)
        return formatted_logs
    except Exception as e:
        logger.error(f"Error fetching directory audit logs for user {user_id}: {str(e)}")
        raise 