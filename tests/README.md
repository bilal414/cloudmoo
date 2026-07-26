# CloudMoo Cloud Provider Testing

This directory contains comprehensive test suites for validating cloud provider connections in CloudMoo. The testing system supports both mocked unit tests and real API integration tests.

## Overview

The testing infrastructure provides:

- **Unit Tests**: Mocked API tests for all cloud providers
- **Integration Tests**: Real API tests (when credentials provided)
- **Configuration Management**: JSON-based test account configuration
- **Management Commands**: Easy setup and teardown of test accounts

## File Structure

```
tests/
├── README.md                    # This documentation
├── test_accounts.json           # Test account configurations and mock responses
├── utils.py                     # Testing utilities and helper classes
├── test_integration.py          # Integration tests with real APIs
├── setup_test_accounts.py       # Management command
├── test_digitalocean_connection.py
├── test_aws_connection.py
├── test_vultr_connection.py
├── test_hetzner_connection.py
├── test_linode_connection.py
└── test_upcloud_connection.py
```

## Quick Start

### 1. Configure Test Accounts

Edit `test_accounts.json` to add your test credentials:

```json
{
  "test_accounts": {
    "digitalocean": {
      "valid": {
        "name": "Test DigitalOcean Account",
        "access_token": "dop_v1_your_real_token_here",
        "description": "Valid DigitalOcean API token for testing"
      }
    }
  },
  "test_settings": {
    "use_real_api": false,
    "mock_responses": true
  }
}
```

### 2. Run Unit Tests (Mocked)

Run all connection tests with mocked responses:

```bash
# All cloud providers
python manage.py test tests

# Specific provider
python manage.py test tests.test_digitalocean_connection
python manage.py test tests.test_aws_connection
```

### 3. Run Integration Tests (Real APIs)

To test against real cloud APIs:

1. Add real credentials to `test_accounts.json`
2. Set `"use_real_api": true` in test settings
3. Run integration tests:

```bash
python manage.py test tests.test_integration
```

### 4. Setup Test Database Accounts

Create test accounts in the database:

```bash
# Setup all providers
python manage.py setup_test_accounts

# Specific provider only
python manage.py setup_test_accounts --provider digitalocean

# Dry run (show what would be created)
python manage.py setup_test_accounts --dry-run
```

## Configuration File Format

### Test Accounts Structure

Each provider can have multiple account types:

```json
{
  "test_accounts": {
    "provider_code": {
      "valid": {
        "name": "Account Name",
        "access_token": "token_here",
        "description": "Description"
      },
      "invalid": {
        "name": "Invalid Account",
        "access_token": "invalid_token",
        "description": "For negative testing"
      },
      "expired": {
        "name": "Expired Account", 
        "access_token": "expired_token",
        "description": "For expiry testing"
      }
    }
  }
}
```

### Provider-Specific Fields

Each provider requires different credential fields:

#### DigitalOcean, Vultr, Hetzner, Linode
```json
{
  "name": "Account Name",
  "access_token": "api_token_here"
}
```

#### AWS
```json
{
  "name": "Account Name",
  "access_key": "AKIAIOSFODNN7EXAMPLE",
  "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  "region": "us-east-1"
}
```

#### UpCloud
```json
{
  "name": "Account Name",
  "username": "your_username",
  "password": "your_password"
}
```

### Mock Responses

Define mock API responses for each provider:

```json
{
  "mock_responses": {
    "digitalocean": {
      "account_success": {
        "status_code": 200,
        "response": {
          "account": {
            "email": "test@example.com",
            "status": "active"
          }
        }
      },
      "account_unauthorized": {
        "status_code": 401,
        "response": {
          "message": "Unable to authenticate"
        }
      }
    }
  }
}
```

## Test Types

### Unit Tests (Mocked)

Test cloud provider validation logic without making real API calls:

- ✅ Valid credential scenarios
- ❌ Invalid credential scenarios  
- 🚫 Rate limiting scenarios
- ⏱️ Timeout scenarios
- 🔒 Permission denied scenarios

Example:
```python
def test_valid_token_connection(self, mock_get):
    account_config = self.get_test_account('digitalocean', 'valid')
    mock_get.return_value = self.create_mock_response('digitalocean', 'account_success')
    
    # Test validation logic
    result = account.validate()
    self.assertTrue(result)
```

### Integration Tests (Real APIs)

Test actual connectivity to cloud provider APIs:

- Only runs when `use_real_api: true`
- Requires real credentials in configuration
- Validates actual API connectivity
- Useful for catching API changes

Example:
```python
@skip_if_no_real_credentials('digitalocean')
def test_real_api_connection(self):
    account_config = self.get_test_account('digitalocean', 'valid')
    result = account.validate()  # Makes real API call
    self.assertTrue(result)
```

## Utility Classes

### TestAccountManager

Manages test account configuration:

```python
from tests.utils import TestAccountManager

# Get test account config
account = TestAccountManager.get_account('digitalocean', 'valid')

# Get mock response
mock = TestAccountManager.create_mock_response('digitalocean', 'account_success')
```

### CloudTestMixin

Base test class with common utilities:

```python
from tests.utils import CloudTestMixin

class MyCloudTest(CloudTestMixin, TestCase):
    def test_something(self):
        account_config = self.get_test_account('aws', 'valid')
        mock_response = self.create_mock_response('aws', 'success')
```

## Security Considerations

### Credential Management

**DO NOT commit real credentials to version control!**

1. Use placeholder values in the committed `test_accounts.json`
2. Create a local `test_accounts.local.json` for real credentials
3. Add `*.local.json` to `.gitignore`
4. Use environment variables for CI/CD

### Safe Testing

- Integration tests include safety checks to prevent running with placeholder credentials
- Use separate test accounts with minimal permissions
- Never use production credentials for testing
- Regularly rotate test account credentials

### Environment Variables

You can override configuration with environment variables:

```bash
export CLOUDMOO_TEST_DIGITALOCEAN_TOKEN="your_token"
export CLOUDMOO_TEST_AWS_ACCESS_KEY="your_key"
export CLOUDMOO_TEST_AWS_SECRET_KEY="your_secret"
```

## Management Commands

### setup_test_accounts

Creates test accounts in the database from configuration:

```bash
# Setup all providers
python manage.py setup_test_accounts

# Specific provider
python manage.py setup_test_accounts --provider digitalocean

# Custom config file
python manage.py setup_test_accounts --config /path/to/config.json

# Dry run
python manage.py setup_test_accounts --dry-run

# Overwrite existing
python manage.py setup_test_accounts --overwrite

# Custom account name
python manage.py setup_test_accounts --account-name "My Test Account"
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Cloud Provider Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Setup Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.9
      
      - name: Install dependencies
        run: pip install -r requirements.txt
      
      - name: Run unit tests (mocked)
        run: python manage.py test tests
      
      - name: Run integration tests
        if: github.ref == 'refs/heads/main'
        env:
          CLOUDMOO_TEST_USE_REAL_API: true
          CLOUDMOO_TEST_DIGITALOCEAN_TOKEN: ${{ secrets.DO_TEST_TOKEN }}
        run: python manage.py test tests.test_integration
```

## Troubleshooting

### Common Issues

1. **Config file not found**
   ```
   Configuration file not found: tests/test_accounts.json
   ```
   Solution: Ensure the file exists and path is correct

2. **Invalid JSON**
   ```
   Invalid JSON in configuration file
   ```
   Solution: Validate JSON syntax with a JSON linter

3. **Missing account type**
   ```
   Account type 'valid' not found for digitalocean
   ```
   Solution: Add the required account type to configuration

4. **Integration tests skipped**
   ```
   No real API credentials configured or use_real_api is disabled
   ```
   Solution: Set `use_real_api: true` and add real credentials

### Debug Mode

Enable verbose output for debugging:

```python
# In test file
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Test Coverage

Check test coverage for cloud provider connections:

```bash
pip install coverage
coverage run --source='.' manage.py test tests
coverage report -m
coverage html  # Generate HTML report
```

## Best Practices

1. **Always test both success and failure scenarios**
2. **Use descriptive test names that explain the scenario**
3. **Group related tests in the same test class**
4. **Use setUp/tearDown for common test data**
5. **Mock external dependencies to ensure test isolation**
6. **Include edge cases like empty strings, special characters**
7. **Test error handling and exception scenarios**
8. **Document complex test scenarios with comments**
9. **Keep test data separate from test logic**
10. **Regularly update test credentials and mock responses**

## Contributing

When adding new cloud providers:

1. Create test file: `test_newprovider_connection.py`
2. Add provider configuration to `test_accounts.json`
3. Add mock responses for the provider
4. Update `setup_test_accounts.py` command
5. Add provider to integration tests
6. Update this documentation

## Support

For issues with the testing infrastructure:

1. Check this documentation first
2. Verify your configuration file syntax
3. Run tests in verbose mode for debugging
4. Check Django logs for detailed error messages
5. Create an issue with full error details and configuration (sanitized)