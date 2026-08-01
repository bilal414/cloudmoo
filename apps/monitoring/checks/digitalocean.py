import requests

from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_http_error


def check_digitalocean_server_status(unique_id, access_token):
    """Check DigitalOcean server status"""
    api_url = f'https://api.digitalocean.com/v2/droplets/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        current_status = data['droplet']['status']
        return current_status, data
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)


def check_digitalocean_volume_status(unique_id, access_token):
    """Check DigitalOcean volume status"""
    api_url = f'https://api.digitalocean.com/v2/volumes/{unique_id}'
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        current_status = "attached" if len(data['volume']['droplet_ids']) > 0 else "detached"
        return current_status, data
    except requests.exceptions.RequestException as e:
        return classify_http_error(e), str(e)
