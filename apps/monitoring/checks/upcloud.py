import base64

import requests

from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_http_error


def _auth_headers(credentials):
    if credentials.get('api_token'):
        authorization = f"Bearer {credentials['api_token']}"
    else:
        username = credentials['username']
        password = credentials['password']
        basic = base64.b64encode(f"{username}:{password}".encode()).decode()
        authorization = f'Basic {basic}'
    return {
        'Authorization': authorization,
        'Content-Type': 'application/json',
    }


def check_upcloud_server_status(unique_id, credentials):
    """Check UpCloud server status"""
    try:
        headers = _auth_headers(credentials)

        # Get server details
        url = f'https://api.upcloud.com/1.3/server/{unique_id}'
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()

        server_data = response.json().get('server', {})
        current_status = server_data.get('state', 'unknown')

        return current_status, {
            'server': server_data
        }
    except requests.exceptions.HTTPError as e:
        return classify_http_error(e), str(e)
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)
    except Exception as e:
        return 'error', str(e)


def check_upcloud_volume_status(unique_id, credentials):
    """Check UpCloud volume status"""
    try:
        headers = _auth_headers(credentials)

        # Get volume details
        url = f'https://api.upcloud.com/1.3/storage/{unique_id}'
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()

        volume_data = response.json().get('storage', {})
        current_status = volume_data.get('state', 'unknown')

        return current_status, {
            'volume': volume_data
        }
    except requests.exceptions.HTTPError as e:
        return classify_http_error(e), str(e)
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)
    except Exception as e:
        return 'error', str(e)
