import requests


def check_linode_server_status(unique_id, access_token):
    """Check Linode server status"""
    api_url = f'https://api.linode.com/v4/linode/instances/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        current_status = data['status']
        return current_status, data
    except requests.exceptions.RequestException as e:
        error_status = 'not_found' if e.response.status_code == 404 else 'invalid_access_token' if e.response.status_code == 401 else 'error'
        return error_status, str(e)


def check_linode_volume_status(unique_id, access_token):
    """Check Linode volume status"""
    api_url = f'https://api.linode.com/v4/volumes/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        # Determine volume status based on whether it's attached to a Linode
        if data.get('linode_id'):
            current_status = "attached"
        else:
            current_status = "detached"

        return current_status, data
    except requests.exceptions.RequestException as e:
        error_status = 'not_found' if e.response.status_code == 404 else 'invalid_access_token' if e.response.status_code == 401 else 'error'
        return error_status, str(e)