import oci
from oci.exceptions import ServiceError


def _oci_config(credentials):
    return {
        'tenancy': credentials['tenancy_ocid'],
        'user': credentials['user_ocid'],
        'fingerprint': credentials['fingerprint'],
        'region': credentials['region'],
        'key_content': credentials['private_key'],
    }


def _classify_service_error(error):
    """Map an OCI service error to CloudMoo's normalized status."""
    status = getattr(error, 'status', None)
    if status == 404:
        return 'not_found'
    if status in (401, 403):
        return 'invalid_access_token'
    return 'error'


def check_oracle_server_status(unique_id, credentials):
    """Check Oracle Cloud compute instance status"""
    try:
        compute_client = oci.core.ComputeClient(_oci_config(credentials))
        instance = compute_client.get_instance(unique_id).data
        current_status = instance.lifecycle_state

        return current_status, {
            'server': oci.util.to_dict(instance)
        }
    except ServiceError as e:
        return _classify_service_error(e), str(e)
    except Exception as e:
        return 'error', str(e)


def check_oracle_volume_status(unique_id, credentials):
    """Check Oracle Cloud block volume status"""
    try:
        blockstorage_client = oci.core.BlockstorageClient(_oci_config(credentials))
        volume = blockstorage_client.get_volume(unique_id).data
        current_status = volume.lifecycle_state

        return current_status, {
            'volume': oci.util.to_dict(volume)
        }
    except ServiceError as e:
        return _classify_service_error(e), str(e)
    except Exception as e:
        return 'error', str(e)
