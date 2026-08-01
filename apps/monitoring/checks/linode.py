import requests

from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_http_error


def check_linode_server_status(unique_id, access_token):
    """Check Linode server status"""
    api_url = f'https://api.linode.com/v4/linode/instances/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        current_status = data['status']
        return current_status, data
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)


def check_linode_volume_status(unique_id, access_token):
    """Check Linode volume status"""
    api_url = f'https://api.linode.com/v4/volumes/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()

        # Determine volume status based on whether it's attached to a Linode
        if data.get('linode_id'):
            current_status = "attached"
        else:
            current_status = "detached"

        return current_status, data
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)
