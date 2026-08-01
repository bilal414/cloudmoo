import requests

from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_http_error


def check_vultr_server_status(unique_id, access_token):
    """Check Vultr server status"""
    api_url = f'https://api.vultr.com/v2/instances/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        current_status = data['instance']['power_status']
        return current_status, data
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)


def check_vultr_volume_status(unique_id, access_token):
    """Check Vultr volume status"""
    api_url = f'https://api.vultr.com/v2/blocks/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        current_status = data['block']['status']
        return current_status, data
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)
