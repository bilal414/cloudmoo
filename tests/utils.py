import json
import os
from django.conf import settings
from unittest.mock import Mock


class TestAccountManager:
    """
    Utility class for managing test accounts configuration and mock responses
    """
    _config = None
    _config_path = None

    @classmethod
    def load_config(cls, config_path=None):
        """Load test accounts configuration from JSON file"""
        if cls._config and cls._config_path == config_path:
            return cls._config

        if not config_path:
            config_path = os.path.join(settings.BASE_DIR, 'tests', 'test_accounts.json')
        
        cls._config_path = config_path
        
        try:
            with open(config_path, 'r') as f:
                cls._config = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Test accounts configuration file not found: {config_path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in test accounts configuration: {e}")
        
        return cls._config

    @classmethod
    def get_account(cls, provider, account_type='valid'):
        """
        Get test account configuration for a specific provider and type
        
        Args:
            provider (str): Cloud provider code (digitalocean, aws, vultr, etc.)
            account_type (str): Account type (valid, invalid, expired, etc.)
            
        Returns:
            dict: Account configuration
        """
        config = cls.load_config()
        
        if provider not in config['test_accounts']:
            raise ValueError(f"No test accounts configured for provider: {provider}")
        
        if account_type not in config['test_accounts'][provider]:
            available_types = list(config['test_accounts'][provider].keys())
            raise ValueError(
                f"Account type '{account_type}' not found for {provider}. "
                f"Available types: {available_types}"
            )
        
        return config['test_accounts'][provider][account_type]

    @classmethod
    def get_all_providers(cls):
        """Get list of all configured providers"""
        config = cls.load_config()
        return list(config['test_accounts'].keys())

    @classmethod
    def get_account_types(cls, provider):
        """Get list of account types for a provider"""
        config = cls.load_config()
        if provider not in config['test_accounts']:
            return []
        return list(config['test_accounts'][provider].keys())

    @classmethod
    def get_mock_response(cls, provider, response_type):
        """
        Get mock response configuration for a provider
        
        Args:
            provider (str): Cloud provider code
            response_type (str): Response type (account_success, account_unauthorized, etc.)
            
        Returns:
            dict: Mock response configuration
        """
        config = cls.load_config()
        
        if 'mock_responses' not in config:
            return None
            
        if provider not in config['mock_responses']:
            return None
            
        return config['mock_responses'][provider].get(response_type)

    @classmethod
    def create_mock_response(cls, provider, response_type):
        """
        Create a Mock object for HTTP responses based on configuration
        
        Args:
            provider (str): Cloud provider code
            response_type (str): Response type
            
        Returns:
            Mock: Configured mock response object
        """
        mock_config = cls.get_mock_response(provider, response_type)
        if not mock_config:
            # Return a generic mock if no config found
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {}
            return mock_response
        
        mock_response = Mock()
        mock_response.status_code = mock_config['status_code']
        
        if 'response' in mock_config:
            mock_response.json.return_value = mock_config['response']
        elif 'error' in mock_config:
            # For AWS-style errors
            from botocore.exceptions import ClientError
            if provider == 'aws':
                error_response = {
                    'Error': {
                        'Code': mock_config['error'],
                        'Message': mock_config.get('message', 'Test error')
                    }
                }
                mock_response.side_effect = ClientError(error_response, 'TestOperation')
                return mock_response
        
        return mock_response

    @classmethod
    def get_test_settings(cls):
        """Get global test settings"""
        config = cls.load_config()
        return config.get('test_settings', {})


class CloudTestMixin:
    """
    Mixin class for cloud provider tests that provides common test utilities
    """
    
    def setUp(self):
        """Setup method that can be called by test classes"""
        super().setUp()
        self.test_manager = TestAccountManager()
        
    def get_test_account(self, provider, account_type='valid'):
        """Get test account for provider"""
        return self.test_manager.get_account(provider, account_type)
        
    def create_mock_response(self, provider, response_type):
        """Create mock response for provider"""
        return self.test_manager.create_mock_response(provider, response_type)
        
    def assert_api_called_with_credentials(self, mock_request, provider, account_config):
        """Assert that API was called with correct credentials"""
        if provider == 'digitalocean':
            expected_headers = {'Authorization': f'Bearer {account_config["access_token"]}'}
            mock_request.assert_called_with(
                'https://api.digitalocean.com/v2/account',
                headers=expected_headers,
                timeout=10
            )
        elif provider == 'vultr':
            expected_headers = {
                'Authorization': f'Bearer {account_config["access_token"]}',
                'Content-Type': 'application/json'
            }
            mock_request.assert_called_with(
                'https://api.vultr.com/v2/account',
                headers=expected_headers,
                timeout=10
            )
        elif provider == 'hetzner':
            expected_headers = {'Authorization': f'Bearer {account_config["access_token"]}'}
            mock_request.assert_called_with(
                'https://api.hetzner.cloud/v1/servers',
                headers=expected_headers,
                timeout=10
            )
        elif provider == 'linode':
            expected_headers = {'Authorization': f'Bearer {account_config["access_token"]}'}
            mock_request.assert_called_with(
                'https://api.linode.com/v4/account',
                headers=expected_headers,
                timeout=10
            )
        # AWS and UpCloud have different assertion patterns handled in specific tests


def skip_if_no_real_credentials(provider, account_type='valid'):
    """
    Decorator to skip tests if real credentials are not available
    """
    def decorator(test_func):
        def wrapper(*args, **kwargs):
            try:
                account = TestAccountManager.get_account(provider, account_type)
                test_settings = TestAccountManager.get_test_settings()
                
                # Skip if we're not supposed to use real API or credentials look fake
                if not test_settings.get('use_real_api', False):
                    return  # Skip the test
                
                # Check if credentials look real (basic validation)
                if provider == 'digitalocean' and account['access_token'].startswith('dop_v1_your_test'):
                    return  # Skip - placeholder token
                elif provider == 'aws' and account['access_key'] == 'AKIAIOSFODNN7EXAMPLE':
                    return  # Skip - example AWS keys
                # Add more validation as needed
                
            except (ValueError, FileNotFoundError):
                return  # Skip if config not found
                
            return test_func(*args, **kwargs)
        return wrapper
    return decorator


def get_integration_test_data():
    """
    Get test data for integration tests that actually call cloud APIs
    Only returns accounts marked as safe for real API calls
    """
    try:
        manager = TestAccountManager()
        config = manager.load_config()
        test_settings = config.get('test_settings', {})
        
        if not test_settings.get('use_real_api', False):
            return {}
            
        integration_accounts = {}
        for provider, accounts in config['test_accounts'].items():
            # Only include 'valid' accounts for integration tests
            if 'valid' in accounts:
                account = accounts['valid']
                # Add basic validation to ensure it's not a placeholder
                is_real = True
                
                if provider == 'digitalocean' and account['access_token'].startswith('dop_v1_your_test'):
                    is_real = False
                elif provider == 'aws' and account['access_key'] == 'AKIAIOSFODNN7EXAMPLE':
                    is_real = False
                    
                if is_real:
                    integration_accounts[provider] = account
                    
        return integration_accounts
        
    except (FileNotFoundError, ValueError):
        return {}