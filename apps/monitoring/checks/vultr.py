import requests


def check_vultr_server_status(unique_id, access_token):
    """Check Vultr server status"""
    api_url = f'https://api.vultr.com/v2/instances/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        current_status = data['instance']['power_status']
        return current_status, data
    except requests.exceptions.RequestException as e:
        error_status = 'not_found' if e.response.status_code == 404 else 'invalid_access_token' if e.response.status_code == 401 else 'error'
        return error_status, str(e)


def check_vultr_volume_status(unique_id, access_token):
    """Check Vultr volume status"""
    api_url = f'https://api.vultr.com/v2/blocks/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        current_status = data['block']['status']
        return current_status, data
    except requests.exceptions.RequestException as e:
        error_status = 'not_found' if e.response.status_code == 404 else 'invalid_access_token' if e.response.status_code == 401 else 'error'
        return error_status, str(e)
