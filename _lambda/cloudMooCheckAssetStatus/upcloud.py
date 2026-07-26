# upcloud.py
import base64
import requests
from datetime import datetime


def check_upcloud_server_status(unique_id, credentials):
    """Check UpCloud server status"""
    try:
        # Parse credentials
        username = credentials['username']
        password = credentials['password']

        # Create auth header
        auth_token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers = {
            'Authorization': f'Basic {auth_token}',
            'Content-Type': 'application/json'
        }

        # Get server details
        url = f'https://api.upcloud.com/1.3/server/{unique_id}'
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        server_data = response.json().get('server', {})
        current_status = server_data.get('state', 'unknown')

        return current_status, {
            'server': server_data
        }
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return 'not_found', str(e)
        elif e.response.status_code == 401:
            return 'invalid_access_token', str(e)
        else:
            return 'error', str(e)
    except Exception as e:
        return 'error', str(e)


def check_upcloud_volume_status(unique_id, credentials):
    """Check UpCloud volume status"""
    try:
        # Parse credentials
        username = credentials['username']
        password = credentials['password']

        # Create auth header
        auth_token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers = {
            'Authorization': f'Basic {auth_token}',
            'Content-Type': 'application/json'
        }

        # Get volume details
        url = f'https://api.upcloud.com/1.3/storage/{unique_id}'
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        volume_data = response.json().get('storage', {})
        current_status = volume_data.get('state', 'unknown')

        return current_status, {
            'volume': volume_data
        }
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return 'not_found', str(e)
        elif e.response.status_code == 401:
            return 'invalid_access_token', str(e)
        else:
            return 'error', str(e)
    except Exception as e:
        return 'error', str(e)