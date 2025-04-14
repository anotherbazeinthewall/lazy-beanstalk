# modules/ship.py

"""
Handles deployment of Elastic Beanstalk application and associated AWS resources.
"""

import os
import tempfile
import zipfile
import time
import fnmatch
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Set, Optional, Tuple
import yaml
from botocore.exceptions import ClientError

from . import common
from .common import DeploymentError
from .configure import (
    get_project_name, get_project_root, get_aws_clients, 
    get_eb_cli_platform_name, ConfigurationError
)

def create_app_bundle() -> str:
    """Create a ZIP archive of application files based on .ebignore."""
    project_root = get_project_root()
    ebignore_path = project_root / '.ebignore'
    
    # Parse .ebignore file
    patterns = []
    negated_patterns = []
    if ebignore_path.exists():
        with open(ebignore_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if line.startswith('!'):
                        negated_patterns.append(line[1:])
                    else:
                        patterns.append(line)
    
    # Create bundle
    bundle_path = Path(tempfile.gettempdir()) / f'app_bundle_{datetime.now():%Y%m%d_%H%M%S}.zip'
    file_count = 0
    
    with zipfile.ZipFile(bundle_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(str(project_root)):
            for filename in files:
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, str(project_root))
                
                # Convert Windows paths to forward slashes for pattern matching
                rel_path_normalized = rel_path.replace(os.sep, '/')
                
                # Check if file should be excluded
                excluded = False
                for pattern in patterns:
                    # Handle directory patterns ending with slash
                    if pattern.endswith('/'):
                        pattern = pattern + '**'
                    # Handle single directory matching
                    if '/' not in pattern and '/' in rel_path_normalized:
                        dirs = rel_path_normalized.split('/')
                        if any(fnmatch.fnmatch(d, pattern) for d in dirs):
                            excluded = True
                            break
                    # Direct pattern match
                    elif fnmatch.fnmatch(rel_path_normalized, pattern):
                        excluded = True
                        break
                    # Handle ** patterns
                    elif '**' in pattern:
                        parts = pattern.split('**')
                        if (pattern.startswith('**') and rel_path_normalized.endswith(parts[1])) or \
                           (pattern.endswith('**') and rel_path_normalized.startswith(parts[0])):
                            excluded = True
                            break
                
                # Check if excluded file should be re-included
                if excluded:
                    for pattern in negated_patterns:
                        if fnmatch.fnmatch(rel_path_normalized, pattern):
                            excluded = False
                            break
                
                # Add file if not excluded
                if not excluded and not rel_path == '.ebignore':
                    zipf.write(file_path, rel_path)
                    file_count += 1
    
    print(f"Created application bundle with {file_count} files")
    return str(bundle_path)

@common.aws_handler
def ensure_instance_profile(iam_client, config: Dict[str, Any]) -> None:
    """Set up instance profile and its associated role."""
    profile_name = config['iam']['instance_profile_name']
    role_name = config['iam']['instance_role_name']
    
    # Set up the role first
    common.manage_iam_role(iam_client, role_name, config['iam']['instance_role_policies'])
    
    try:
        iam_client.get_instance_profile(InstanceProfileName=profile_name)
        profile = iam_client.get_instance_profile(InstanceProfileName=profile_name)
        # Check if role is attached
        roles = profile['InstanceProfile'].get('Roles', [])
        if not roles or roles[0]['RoleName'] != role_name:
            # Remove any existing roles
            for existing_role in roles:
                iam_client.remove_role_from_instance_profile(
                    InstanceProfileName=profile_name,
                    RoleName=existing_role['RoleName']
                )
            # Add our role
            iam_client.add_role_to_instance_profile(
                InstanceProfileName=profile_name,
                RoleName=role_name
            )
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchEntity':
            iam_client.create_instance_profile(InstanceProfileName=profile_name)
            iam_client.add_role_to_instance_profile(
                InstanceProfileName=profile_name,
                RoleName=role_name
            )
            time.sleep(10)  # Allow time for profile propagation
        else:
            raise

def wait_for_version(eb_client, app_name: str, version: str) -> None:
    """Wait for application version to be processed."""
    print("Waiting for application version to be processed...")
    while True:
        versions = eb_client.describe_application_versions(
            ApplicationName=app_name,
            VersionLabels=[version]
        )['ApplicationVersions']
        
        if not versions:
            raise DeploymentError(f"Version {version} not found")
        
        status = versions[0]['Status']
        print(f"Version status: {status}")
        
        if status == 'PROCESSED':
            break
        elif status == 'FAILED':
            raise DeploymentError(f"Version {version} processing failed")
        time.sleep(5)

@common.aws_handler
def preserve_env_state(eb_client, elbv2_client, env_name: str, project_name: str) -> Optional[Dict[str, Any]]:
    """
    Preserve environment state before update, including HTTPS configuration.
    
    Args:
        eb_client: Elastic Beanstalk client
        elbv2_client: ELBv2 client
        env_name: Environment name
        project_name: Project name for tag prefix
    
    Returns:
        Dictionary containing state to preserve, or None if no state to preserve
    """
    lb_arn = common.find_environment_load_balancer(eb_client, elbv2_client, env_name)
    if not lb_arn:
        return None

    https_config = common.preserve_https_config(elbv2_client, lb_arn, project_name)
    if not https_config:
        return None

    return {
        'https_config': https_config,
        'load_balancer_arn': lb_arn
    }

def restore_env_state(elbv2_client, state: Dict[str, Any], project_name: str) -> None:
    """
    Restore environment state after update, including HTTPS configuration.
    
    Args:
        elbv2_client: ELBv2 client
        state: State dictionary from preserve_env_state
        project_name: Project name for tag prefix
    """
    if not state:
        return

    if 'https_config' in state and state['https_config']:
        print("Restoring HTTPS configuration...")
        common.restore_https_config(
            elbv2_client,
            state['load_balancer_arn'],
            state['https_config'],
            project_name
        )

def create_or_update_env(eb_client, elbv2_client, config: Dict[str, Any], version: str, project_name: str) -> None:
    """Create or update Elastic Beanstalk environment."""
    env_name = config['application']['environment']
    env_exists = bool(eb_client.describe_environments(
        EnvironmentNames=[env_name],
        IncludeDeleted=False
    )['Environments'])
    
    settings = common.get_env_settings(config)
    state = None
    
    if env_exists:
        print("Updating existing environment...")
        # Preserve state before update
        state = preserve_env_state(eb_client, elbv2_client, env_name, project_name)
        
        eb_client.update_environment(
            EnvironmentName=env_name,
            VersionLabel=version,
            OptionSettings=settings
        )
    else:
        print("Creating new environment...")
        settings.append({
            'Namespace': 'aws:elasticbeanstalk:environment',
            'OptionName': 'LoadBalancerType',
            'Value': config['instance']['elb_type']
        })
        eb_client.create_environment(
            ApplicationName=config['application']['name'],
            EnvironmentName=env_name,
            VersionLabel=version,
            SolutionStackName=config['aws']['platform'],
            OptionSettings=settings
        )
    
    common.wait_for_env_status(eb_client, env_name, 'Ready')
    
    if state:
        restore_env_state(elbv2_client, state, project_name)

def create_eb_cli_config(config: Dict[str, Any], project_root: Path) -> None:
    """
    Create EB CLI configuration file from the main config.yml.
    
    Args:
        config: The loaded and processed config dictionary
        project_root: The root path of the project
    """
    eb_dir = project_root / '.elasticbeanstalk'
    eb_dir.mkdir(exist_ok=True)
    
    # Check if elasticbeanstalk_cli section exists
    if 'elasticbeanstalk_cli' not in config:
        print("Warning: No elasticbeanstalk_cli section found in config.yml")
        # Use the old method to generate the config
        eb_config = {
            'branch-defaults': {
                'main': {
                    'environment': config['application']['environment'],
                    'group_suffix': None
                }
            },
            'global': {
                'application_name': config['application']['name'],
                'branch': None,
                'default_ec2_keyname': None,
                'default_platform': get_eb_cli_platform_name(config['aws']['platform']),
                'default_region': config['aws']['region'],
                'include_git_submodules': True,
                'instance_profile': None,
                'platform_name': None,
                'platform_version': None,
                'profile': None,
                'repository': None,
                'sc': 'git',
                'workspace_type': 'Application'
            }
        }
    else:
        # Use the elasticbeanstalk_cli section from the config
        eb_config = config['elasticbeanstalk_cli']
        
        # Check if EB_CLI_PLATFORM placeholder exists and replace it
        if 'global' in eb_config and 'default_platform' in eb_config['global']:
            platform_value = eb_config['global']['default_platform']
            if isinstance(platform_value, str) and '${EB_CLI_PLATFORM}' in platform_value:
                eb_config['global']['default_platform'] = get_eb_cli_platform_name(config['aws']['platform'])
    
    # Write the config file
    (eb_dir / 'config.yml').write_text(yaml.safe_dump(eb_config, sort_keys=True))
    print(f"Created EB CLI configuration in {eb_dir / 'config.yml'}")

def deploy_application(config: Dict[str, Any]) -> None:
    """Deploy the application to Elastic Beanstalk."""
    project_name = get_project_name()
    region = config['aws']['region']
    platform = config['aws']['platform']
    project_root = get_project_root()
    
    print(f"Deploying to region: {region}")
    print(f"Using platform: {platform}")
    
    # Get AWS clients
    aws_clients = get_aws_clients(config)
    eb_client = aws_clients['eb']
    iam_client = aws_clients['iam']
    s3_client = aws_clients['s3']
    elbv2_client = aws_clients['elbv2']
    
    # Create EB CLI config file
    create_eb_cli_config(config, project_root)
    
    # Set up IAM resources
    common.manage_iam_role(
        iam_client,
        config['iam']['service_role_name'],
        config['iam']['service_role_policies']
    )
    ensure_instance_profile(iam_client, config)
    
    # Create/update application
    app_name = config['application']['name']
    if not eb_client.describe_applications(ApplicationNames=[app_name])['Applications']:
        eb_client.create_application(
            ApplicationName=app_name,
            Description=config.get('application', {}).get('description', 'Application created by deployment script')
        )
    
    # Create and upload application version
    version = f"v{datetime.now():%Y%m%d_%H%M%S}"
    bucket = f"elasticbeanstalk-{region}-{app_name.lower()}"
    
    try:
        s3_client.head_bucket(Bucket=bucket)
    except ClientError:
        # For regions other than us-east-1, we need to specify the LocationConstraint
        create_bucket_args = {'Bucket': bucket}
        if region != 'us-east-1':
            create_bucket_args['CreateBucketConfiguration'] = {'LocationConstraint': region}
        
        s3_client.create_bucket(**create_bucket_args)
    
    bundle = create_app_bundle()
    key = f"app-{version}.zip"
    
    with open(bundle, 'rb') as f:
        s3_client.upload_fileobj(f, bucket, key)
    os.remove(bundle)
    
    eb_client.create_application_version(
        ApplicationName=app_name,
        VersionLabel=version,
        SourceBundle={'S3Bucket': bucket, 'S3Key': key},
        Process=True
    )
    
    wait_for_version(eb_client, app_name, version)
    create_or_update_env(eb_client, elbv2_client, config, version, project_name)