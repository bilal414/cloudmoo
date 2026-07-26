import json
import os

import requests
import logging
from typing import Dict, Any

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda handler function that makes an API call to CloudMoo's asset sync endpoint.

    Args:
        event (dict): The event dict containing the ID to be synced
        context (Any): AWS Lambda context object

    Returns:
        dict: Response containing statusCode and body
    """
    try:
        # Log the incoming event
        logger.info(f"Received event: {json.dumps(event)}")

        # Extract ID from the event
        if not isinstance(event.get('uuid'), (int, str)):
            raise ValueError("Missing or invalid 'uuid' in event")

        # API endpoint configuration
        url = "https://cloudmoo.com/api/v1/webhook/cloud/sync_assets/"
        headers = {
            'X-API-Key': os.environ['CLOUDMOO_API_KEY'],
            'Content-Type': 'application/json'
        }

        # Prepare the payload
        payload = {
            "uuid": event['uuid']
        }

        # Make the API request
        logger.info(f"Making API request to {url} with payload: {payload}")
        response = requests.post(url, headers=headers, json=payload, timeout=30)

        # Raise an exception for bad status codes
        response.raise_for_status()

        # Log the response
        logger.info(f"Received response: {response.text}")

        # Return successful response
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Asset sync triggered successfully',
                'response': response.text
            })
        }

    except requests.exceptions.RequestException as e:
        # Handle request-related errors
        error_message = f"API request failed: {str(e)}"
        logger.error(error_message)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': error_message
            })
        }

    except ValueError as e:
        # Handle validation errors
        error_message = str(e)
        logger.error(error_message)
        return {
            'statusCode': 400,
            'body': json.dumps({
                'error': error_message
            })
        }

    except Exception as e:
        # Handle any other unexpected errors
        error_message = f"Unexpected error: {str(e)}"
        logger.error(error_message)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': error_message
            })
        }