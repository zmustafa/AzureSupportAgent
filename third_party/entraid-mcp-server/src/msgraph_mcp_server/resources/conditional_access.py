"""Conditional Access resource module for Microsoft Graph.

This module provides access to Microsoft Graph conditional access policy resources.
"""

import logging
from typing import Dict, List, Any
from msgraph.generated.identity.conditional_access.policies.policies_request_builder import PoliciesRequestBuilder
from utils.graph_client import GraphClient

logger = logging.getLogger(__name__)

def format_list_for_csv(lst):
    if not lst:
        return ""
    return "; ".join(str(item) for item in lst)

async def get_group_details(client, group_ids):
    group_details = {}
    for group_id in group_ids:
        if not group_id:
            continue
        if group_id in ['All', 'None', 'GuestsOrExternalUsers', 'GuestOrExternalUserTypes']:
            group_details[group_id] = group_id
            continue
        try:
            group = await client.groups.by_group_id(group_id).get()
            group_details[group_id] = f"{getattr(group, 'display_name', group_id)} ({group_id})"
        except Exception as e:
            logger.warning(f"Could not fetch details for group {group_id}: {str(e)}")
            group_details[group_id] = f"Unknown Group ({group_id})"
    return group_details

async def parse_conditions(client, conditions):
    parsed = {
        'Users_Include': [], 'Users_Exclude': [], 'Groups_Include': [], 'Groups_Include_Names': [],
        'Groups_Exclude': [], 'Groups_Exclude_Names': [], 'Roles_Include': [], 'Roles_Exclude': [],
        'Include_Guest_Or_External_Users': '', 'Exclude_Guest_Or_External_Users': '',
        'Apps_Include': [], 'Apps_Exclude': [], 'User_Actions': [], 'Authentication_Context_References': [],
        'Application_Filter': '', 'User_Risk_Levels': [], 'Sign_In_Risk_Levels': [],
        'Service_Principal_Risk_Levels': [], 'Insider_Risk_Levels': '', 'Client_App_Types': [],
        'Platforms': '', 'Locations': '', 'Devices': '', 'Client_Applications': ''
    }
    try:
        if hasattr(conditions, 'user_risk_levels'):
            parsed['User_Risk_Levels'] = conditions.user_risk_levels or []
        if hasattr(conditions, 'sign_in_risk_levels'):
            parsed['Sign_In_Risk_Levels'] = conditions.sign_in_risk_levels or []
        if hasattr(conditions, 'service_principal_risk_levels'):
            parsed['Service_Principal_Risk_Levels'] = conditions.service_principal_risk_levels or []
        if hasattr(conditions, 'insider_risk_levels'):
            parsed['Insider_Risk_Levels'] = conditions.insider_risk_levels or ''
        if hasattr(conditions, 'client_app_types'):
            parsed['Client_App_Types'] = conditions.client_app_types or []
        if hasattr(conditions, 'applications'):
            if hasattr(conditions.applications, 'include_applications'):
                parsed['Apps_Include'] = conditions.applications.include_applications or []
            if hasattr(conditions.applications, 'exclude_applications'):
                parsed['Apps_Exclude'] = conditions.applications.exclude_applications or []
            if hasattr(conditions.applications, 'include_user_actions'):
                parsed['User_Actions'] = conditions.applications.include_user_actions or []
            if hasattr(conditions.applications, 'include_authentication_context_class_references'):
                parsed['Authentication_Context_References'] = conditions.applications.include_authentication_context_class_references or []
            if hasattr(conditions.applications, 'application_filter'):
                parsed['Application_Filter'] = conditions.applications.application_filter or ''
        if hasattr(conditions, 'users'):
            if hasattr(conditions.users, 'include_users'):
                parsed['Users_Include'] = conditions.users.include_users or []
            if hasattr(conditions.users, 'exclude_users'):
                parsed['Users_Exclude'] = conditions.users.exclude_users or []
            if hasattr(conditions.users, 'include_groups'):
                parsed['Groups_Include'] = conditions.users.include_groups or []
            if hasattr(conditions.users, 'exclude_groups'):
                parsed['Groups_Exclude'] = conditions.users.exclude_groups or []
            if hasattr(conditions.users, 'include_roles'):
                parsed['Roles_Include'] = conditions.users.include_roles or []
            if hasattr(conditions.users, 'exclude_roles'):
                parsed['Roles_Exclude'] = conditions.users.exclude_roles or []
            if hasattr(conditions.users, 'include_guests_or_external_users'):
                parsed['Include_Guest_Or_External_Users'] = str(conditions.users.include_guests_or_external_users or '')
            if hasattr(conditions.users, 'exclude_guests_or_external_users'):
                parsed['Exclude_Guest_Or_External_Users'] = str(conditions.users.exclude_guests_or_external_users or '')
        all_groups = list(set(parsed['Groups_Include'] + parsed['Groups_Exclude']))
        if all_groups:
            group_details = await get_group_details(client, all_groups)
            parsed['Groups_Include_Names'] = [group_details.get(group_id, f"Unknown Group ({group_id})") for group_id in parsed['Groups_Include'] if group_id]
            parsed['Groups_Exclude_Names'] = [group_details.get(group_id, f"Unknown Group ({group_id})") for group_id in parsed['Groups_Exclude'] if group_id]
        parsed['Platforms'] = str(conditions.platforms or '')
        parsed['Locations'] = str(conditions.locations or '')
        parsed['Devices'] = str(conditions.devices or '')
        parsed['Client_Applications'] = str(conditions.client_applications or '')
    except Exception as e:
        logger.warning(f"Error parsing conditions: {str(e)}")
    return {k: format_list_for_csv(v) if isinstance(v, list) else v for k, v in parsed.items()}

def parse_grant_controls(grant_controls):
    try:
        if not grant_controls:
            return {
                'Operator': '', 'Built_In_Controls': '', 'Custom_Authentication_Factors': '', 'Terms_Of_Use': '',
                'Auth_Strength_Id': '', 'Auth_Strength_DisplayName': '', 'Auth_Strength_Description': '',
                'Auth_Strength_PolicyType': '', 'Auth_Strength_Requirements': '', 'Auth_Strength_Combinations': ''
            }
        parsed = {}
        parsed['Operator'] = grant_controls.operator if hasattr(grant_controls, 'operator') else ''
        parsed['Built_In_Controls'] = format_list_for_csv(grant_controls.built_in_controls) if hasattr(grant_controls, 'built_in_controls') else ''
        parsed['Custom_Authentication_Factors'] = format_list_for_csv(grant_controls.custom_authentication_factors) if hasattr(grant_controls, 'custom_authentication_factors') else ''
        parsed['Terms_Of_Use'] = format_list_for_csv(grant_controls.terms_of_use) if hasattr(grant_controls, 'terms_of_use') else ''
        if hasattr(grant_controls, 'authentication_strength'):
            auth_strength = grant_controls.authentication_strength
            parsed['Auth_Strength_Id'] = getattr(auth_strength, 'id', '')
            parsed['Auth_Strength_DisplayName'] = getattr(auth_strength, 'display_name', '')
            parsed['Auth_Strength_Description'] = getattr(auth_strength, 'description', '')
            parsed['Auth_Strength_PolicyType'] = getattr(auth_strength, 'policy_type', '')
            parsed['Auth_Strength_Requirements'] = getattr(auth_strength, 'requirements_satisfied', '')
            parsed['Auth_Strength_Combinations'] = format_list_for_csv(getattr(auth_strength, 'allowed_combinations', []))
        else:
            parsed.update({
                'Auth_Strength_Id': '', 'Auth_Strength_DisplayName': '', 'Auth_Strength_Description': '',
                'Auth_Strength_PolicyType': '', 'Auth_Strength_Requirements': '', 'Auth_Strength_Combinations': ''
            })
        return parsed
    except Exception as e:
        logger.warning(f"Error parsing grant controls: {str(e)}")
        return {
            'Operator': '', 'Built_In_Controls': '', 'Custom_Authentication_Factors': '', 'Terms_Of_Use': '',
            'Auth_Strength_Id': '', 'Auth_Strength_DisplayName': '', 'Auth_Strength_Description': '',
            'Auth_Strength_PolicyType': '', 'Auth_Strength_Requirements': '', 'Auth_Strength_Combinations': ''
        }

def parse_session_controls(session_controls):
    try:
        if not session_controls:
            return {
                'Disable_Resilience_Defaults': '', 'Application_Enforced_Restrictions': '', 'Cloud_App_Security': '',
                'Persistent_Browser': '', 'Sign_In_Frequency_Value': '', 'Sign_In_Frequency_Type': '',
                'Sign_In_Frequency_Auth_Type': '', 'Sign_In_Frequency_Interval': '', 'Sign_In_Frequency_IsEnabled': ''
            }
        parsed = {}
        parsed['Disable_Resilience_Defaults'] = str(getattr(session_controls, 'disable_resilience_defaults', ''))
        parsed['Application_Enforced_Restrictions'] = str(getattr(session_controls, 'application_enforced_restrictions', ''))
        parsed['Cloud_App_Security'] = str(getattr(session_controls, 'cloud_app_security', ''))
        parsed['Persistent_Browser'] = str(getattr(session_controls, 'persistent_browser', ''))
        if hasattr(session_controls, 'sign_in_frequency'):
            sign_in_freq = session_controls.sign_in_frequency
            parsed['Sign_In_Frequency_Value'] = str(getattr(sign_in_freq, 'value', ''))
            parsed['Sign_In_Frequency_Type'] = str(getattr(sign_in_freq, 'type', ''))
            parsed['Sign_In_Frequency_Auth_Type'] = str(getattr(sign_in_freq, 'authentication_type', ''))
            parsed['Sign_In_Frequency_Interval'] = str(getattr(sign_in_freq, 'frequency_interval', ''))
            parsed['Sign_In_Frequency_IsEnabled'] = 'Yes' if getattr(sign_in_freq, 'is_enabled', False) else 'No'
        else:
            parsed.update({
                'Sign_In_Frequency_Value': '', 'Sign_In_Frequency_Type': '', 'Sign_In_Frequency_Auth_Type': '',
                'Sign_In_Frequency_Interval': '', 'Sign_In_Frequency_IsEnabled': ''
            })
        return parsed
    except Exception as e:
        logger.warning(f"Error parsing session controls: {str(e)}")
        return {
            'Disable_Resilience_Defaults': '', 'Application_Enforced_Restrictions': '', 'Cloud_App_Security': '',
            'Persistent_Browser': '', 'Sign_In_Frequency_Value': '', 'Sign_In_Frequency_Type': '',
            'Sign_In_Frequency_Auth_Type': '', 'Sign_In_Frequency_Interval': '', 'Sign_In_Frequency_IsEnabled': ''
        }

async def get_conditional_access_policies(graph_client: GraphClient) -> List[Dict[str, Any]]:
    """Get all conditional access policies with comprehensive details."""
    try:
        client = graph_client.get_client()
        policies_response = await client.identity.conditional_access.policies.get()
        policies = []
        if policies_response and policies_response.value:
            for policy in policies_response.value:
                # Parse conditions
                conditions = await parse_conditions(client, getattr(policy, 'conditions', None))
                # Parse grant controls
                grant_controls = parse_grant_controls(getattr(policy, 'grant_controls', None))
                # Parse session controls
                session_controls = parse_session_controls(getattr(policy, 'session_controls', None))
                # Compose policy data
                policy_data = {
                    'id': getattr(policy, 'id', None),
                    'displayName': getattr(policy, 'display_name', None),
                    'state': getattr(policy, 'state', None).value if getattr(policy, 'state', None) else None,
                    'createdDateTime': policy.created_date_time.isoformat() if getattr(policy, 'created_date_time', None) else None,
                    'modifiedDateTime': policy.modified_date_time.isoformat() if getattr(policy, 'modified_date_time', None) else None,
                    **conditions,
                    **{f'Grant_{k}': v for k, v in grant_controls.items()},
                    **session_controls
                }
                policies.append(policy_data)
        return policies
    except Exception as e:
        logger.error(f"Error fetching conditional access policies: {str(e)}")
        raise

async def get_conditional_access_policy_by_id(graph_client: GraphClient, policy_id: str) -> Dict[str, Any]:
    """Get a single conditional access policy by its ID with comprehensive details."""
    try:
        client = graph_client.get_client()
        policy = await client.identity.conditional_access.policies.by_conditional_access_policy_id(policy_id).get()
        if not policy:
            return {}
        # Parse conditions
        conditions = await parse_conditions(client, getattr(policy, 'conditions', None))
        # Parse grant controls
        grant_controls = parse_grant_controls(getattr(policy, 'grant_controls', None))
        # Parse session controls
        session_controls = parse_session_controls(getattr(policy, 'session_controls', None))
        # Compose policy data
        policy_data = {
            'id': getattr(policy, 'id', None),
            'displayName': getattr(policy, 'display_name', None),
            'state': getattr(policy, 'state', None).value if getattr(policy, 'state', None) else None,
            'createdDateTime': policy.created_date_time.isoformat() if getattr(policy, 'created_date_time', None) else None,
            'modifiedDateTime': policy.modified_date_time.isoformat() if getattr(policy, 'modified_date_time', None) else None,
            **conditions,
            **{f'Grant_{k}': v for k, v in grant_controls.items()},
            **session_controls
        }
        return policy_data
    except Exception as e:
        logger.error(f"Error fetching conditional access policy by ID {policy_id}: {str(e)}")
        raise 