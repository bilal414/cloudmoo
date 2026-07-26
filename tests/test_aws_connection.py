from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch, Mock
from botocore.exceptions import ClientError, NoCredentialsError
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.account.models import CoreAccount
from tests.utils import CloudTestMixin, TestAccountManager, skip_if_no_real_credentials


class AWSConnectionTestCase(CloudTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Create test user first
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.account = CoreAccount.objects.create(
            name="Test Account",
            status=CoreAccount.Status.ACTIVE,
            owner=self.user
        )
        self.provider, _ = CoreCloudServiceProvider.objects.get_or_create(
            code="aws",
            defaults={
                "name": "Amazon Web Services",
                "status": CoreCloudServiceProvider.Status.ACTIVE
            }
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE
        )

    @patch('boto3.Session')
    def test_valid_credentials_connection(self, mock_session):
        # Get test account configuration
        account_config = self.get_test_account('aws', 'valid')
        
        mock_session_instance = Mock()
        mock_ec2_client = Mock()
        mock_ec2_client.describe_instances.return_value = {"Reservations": []}
        mock_session_instance.client.return_value = mock_ec2_client
        mock_session.return_value = mock_session_instance

        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_key=account_config['access_key'],
            secret_key=account_config['secret_key'],
            region=account_config['region']
        )

        result = aws_account.validate()
        self.assertTrue(result)
        
        mock_session.assert_called_once_with(
            aws_access_key_id=account_config['access_key'],
            aws_secret_access_key=account_config['secret_key'],
            region_name=account_config['region']
        )
        mock_ec2_client.describe_instances.assert_called_once()

    @patch('boto3.Session')
    def test_invalid_credentials_connection(self, mock_session):
        # Get invalid test account configuration
        account_config = self.get_test_account('aws', 'invalid')
        
        mock_session_instance = Mock()
        mock_ec2_client = Mock()
        mock_ec2_client.describe_instances.side_effect = ClientError(
            error_response={'Error': {'Code': 'InvalidUserID.NotFound'}},
            operation_name='DescribeInstances'
        )
        mock_session_instance.client.return_value = mock_ec2_client
        mock_session.return_value = mock_session_instance

        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_key=account_config['access_key'],
            secret_key=account_config['secret_key'],
            region=account_config['region']
        )

        result = aws_account.validate()
        self.assertFalse(result)

    @patch('boto3.Session')
    def test_no_credentials_connection(self, mock_session):
        mock_session.side_effect = NoCredentialsError()

        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name="Test AWS Account",
            access_key="",
            secret_key="",
            region="us-east-1"
        )

        result = aws_account.validate()
        self.assertFalse(result)

    @patch('boto3.Session')
    def test_invalid_region_connection(self, mock_session):
        # Get invalid region test account configuration
        account_config = self.get_test_account('aws', 'invalid_region')
        
        mock_session_instance = Mock()
        mock_ec2_client = Mock()
        mock_ec2_client.describe_instances.side_effect = ClientError(
            error_response={'Error': {'Code': 'InvalidRegion'}},
            operation_name='DescribeInstances'
        )
        mock_session_instance.client.return_value = mock_ec2_client
        mock_session.return_value = mock_session_instance

        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_key=account_config['access_key'],
            secret_key=account_config['secret_key'],
            region=account_config['region']
        )

        result = aws_account.validate()
        self.assertFalse(result)

    @patch('boto3.Session')
    def test_access_denied_connection(self, mock_session):
        mock_session_instance = Mock()
        mock_ec2_client = Mock()
        mock_ec2_client.describe_instances.side_effect = ClientError(
            error_response={'Error': {'Code': 'UnauthorizedOperation'}},
            operation_name='DescribeInstances'
        )
        mock_session_instance.client.return_value = mock_ec2_client
        mock_session.return_value = mock_session_instance

        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name="Test AWS Account",
            access_key="AKIATEST123",
            secret_key="test_secret_key_123",
            region="us-east-1"
        )

        result = aws_account.validate()
        self.assertFalse(result)

    def test_access_token_property(self):
        account_config = self.get_test_account('aws', 'valid')
        
        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_key=account_config['access_key'],
            secret_key=account_config['secret_key'],
            region=account_config['region']
        )

        expected_token = {
            'access_key': account_config['access_key'],
            'secret_key': account_config['secret_key'],
            'region': account_config['region']
        }
        
        self.assertEqual(aws_account.access_token, expected_token)

    @skip_if_no_real_credentials('aws')
    def test_real_api_connection(self):
        """
        Integration test with real AWS API (only runs if real credentials provided)
        """
        account_config = self.get_test_account('aws', 'valid')
        
        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_key=account_config['access_key'],
            secret_key=account_config['secret_key'],
            region=account_config['region']
        )

        # This will make actual API call if real credentials are provided
        result = aws_account.validate()
        self.assertTrue(result, "Real AWS API validation should succeed with valid credentials")

    @patch('boto3.Session')
    def test_network_timeout_connection(self, mock_session):
        mock_session.side_effect = Exception("Connection timeout")

        aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name="Test AWS Account",
            access_key="AKIATEST123",
            secret_key="test_secret_key_123",
            region="us-east-1"
        )

        result = aws_account.validate()
        self.assertFalse(result)