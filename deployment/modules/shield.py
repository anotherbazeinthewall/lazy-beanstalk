# shield.py

import os
import getpass
from typing import Dict, Optional, Tuple

from .support import DeploymentError, aws_handler
from .setup import (
    ConfigurationManager,
    ClientManager,
    logger,
    ensure_env_in_gitignore,
    EnvironmentManager,
)


def check_oidc_vars(config):
    """
    Check for missing OIDC variables and provide instructions for setting them.

    Args:
        config: Configuration dictionary

    Returns:
        bool: True if all required variables are present, False otherwise
    """
    # Define required environment variables
    required_vars = [
        ("client_id", "LB_OIDC_CLIENT_ID"),
        ("client_secret", "LB_OIDC_CLIENT_SECRET"),
        ("issuer", "LB_OIDC_ISSUER"),
        ("endpoints.authorization", "LB_OIDC_AUTH_ENDPOINT"),
        ("endpoints.token", "LB_OIDC_TOKEN_ENDPOINT"),
        ("endpoints.userinfo", "LB_OIDC_USERINFO_ENDPOINT"),
    ]

    # Check for old variable names in environment and map them
    var_mapping = EnvironmentManager.get_old_to_new_env_mapping()
    for old_name, new_name in var_mapping.items():
        if old_name in os.environ and new_name not in os.environ:
            os.environ[new_name] = os.environ[old_name]
            logger.debug(f"Mapped {old_name} to {new_name} in environment")

    # Find which variables are missing
    missing = []
    for config_path, env_var in required_vars:
        if env_var not in os.environ or not os.environ[env_var]:
            missing.append(env_var)

    if not missing:
        return True  # All variables are present

    # Provide instructions for missing variables
    logger.error("Missing required OIDC configuration variables:")
    for var in missing:
        logger.error(f"  - {var}")

    logger.info("\nPlease set these variables in your .env file or environment.")
    logger.info("Example .env file format:")
    logger.info("----------------------------------------------------")
    for config_path, env_var in required_vars:
        if env_var in missing:
            logger.info(f"{env_var}=your-value-here")
    logger.info("----------------------------------------------------")

    # Ensure .env is in .gitignore
    ensure_env_in_gitignore()

    return False


def validate_oidc_config(config: Dict) -> bool:
    """
    Validate OIDC configuration and provide helpful error messages.

    Args:
        config: The loaded configuration dictionary

    Returns:
        bool: True if configuration is valid, False otherwise
    """
    if "oidc" not in config:
        logger.error("Missing 'oidc' section in configuration.")
        return False

    # Check for required variables
    if not check_oidc_vars(config):
        return False

    logger.info("OIDC configuration validated successfully")
    return True


@aws_handler
def get_domain_from_listener(listener_arn: str) -> str:
    """Extract domain from the certificate attached to the HTTPS listener."""
    elbv2_client = ClientManager.get_client("elbv2")
    listener = elbv2_client.describe_listeners(ListenerArns=[listener_arn])[
        "Listeners"
    ][0]
    cert_arn = listener["Certificates"][0]["CertificateArn"]

    acm_client = ClientManager.get_client("acm")
    cert = acm_client.describe_certificate(CertificateArn=cert_arn)["Certificate"]
    project_name = ConfigurationManager.get_project_name()
    return cert["DomainName"].replace("*", project_name)


@aws_handler
def find_listener_arn(env_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Find HTTPS listener ARN for the environment's ALB."""
    eb_client = ClientManager.get_client("elasticbeanstalk")
    elbv2_client = ClientManager.get_client("elbv2")

    logger.info("Finding HTTPS listener")

    # Check if environment exists
    envs = eb_client.describe_environments(
        EnvironmentNames=[env_name], IncludeDeleted=False
    )["Environments"]
    if not envs:
        logger.info("environment not found")
        raise DeploymentError(f"Environment {env_name} not found")

    # Find the load balancer for the environment
    lbs = elbv2_client.describe_load_balancers()["LoadBalancers"]
    env_lb = None

    for lb in lbs:
        if lb["Type"].lower() != "application":
            continue

        tags = elbv2_client.describe_tags(ResourceArns=[lb["LoadBalancerArn"]])[
            "TagDescriptions"
        ][0]["Tags"]
        if any(
            t["Key"] == "elasticbeanstalk:environment-name" and t["Value"] == env_name
            for t in tags
        ):
            env_lb = lb
            break

    if not env_lb:
        logger.info("load balancer not found")
        raise DeploymentError(f"No load balancer found for environment {env_name}")

    # Find the HTTPS listener
    listeners = elbv2_client.describe_listeners(
        LoadBalancerArn=env_lb["LoadBalancerArn"]
    )["Listeners"]
    https_listener = next((l for l in listeners if l["Port"] == 443), None)

    if not https_listener:
        logger.info("HTTPS not configured")
        raise DeploymentError("HTTPS listener not found. Run 'secure' command first.")

    return https_listener["ListenerArn"], env_lb["LoadBalancerArn"]


@aws_handler
def find_target_group_arn(load_balancer_arn: str) -> str:
    """Get target group ARN for the load balancer."""
    elbv2_client = ClientManager.get_client("elbv2")
    logger.info("Finding target group")

    target_groups = elbv2_client.describe_target_groups(
        LoadBalancerArn=load_balancer_arn
    )["TargetGroups"]
    if not target_groups:
        logger.info("not found")
        raise DeploymentError("No target groups found for load balancer")

    return target_groups[0]["TargetGroupArn"]


def get_client_secret(secret: Optional[str] = None) -> str:
    """Get client secret from args, environment, or prompt user."""
    # Priority: 1. Command line arg, 2. Environment variable, 3. Interactive prompt
    if secret:
        return secret
    if secret := os.environ.get("LB_OIDC_CLIENT_SECRET"):
        return secret
    # Backward compatibility - check for old environment variable
    if secret := os.environ.get("OIDC_CLIENT_SECRET"):
        return secret
    return getpass.getpass("\nPlease enter your OIDC client secret: ")


@aws_handler
def configure_oidc_auth(config: Dict, client_secret: Optional[str] = None) -> None:
    """
    Configure OIDC authentication on ALB listener.

    Args:
        config: The loaded configuration dictionary
        client_secret: Optional client secret (will be prompted if not provided)
    """
    env_name = config["application"]["environment"]

    # Prioritize environment variables over config file values using new prefixed names
    oidc_config = {
        "client_id": os.environ.get("LB_OIDC_CLIENT_ID", config["oidc"]["client_id"]),
        "client_secret": client_secret or os.environ.get("LB_OIDC_CLIENT_SECRET", ""),
        "issuer": os.environ.get("LB_OIDC_ISSUER", config["oidc"]["issuer"]),
        "endpoints": {
            "authorization": os.environ.get(
                "LB_OIDC_AUTH_ENDPOINT", config["oidc"]["endpoints"]["authorization"]
            ),
            "token": os.environ.get(
                "LB_OIDC_TOKEN_ENDPOINT", config["oidc"]["endpoints"]["token"]
            ),
            "userinfo": os.environ.get(
                "LB_OIDC_USERINFO_ENDPOINT", config["oidc"]["endpoints"]["userinfo"]
            ),
        },
        "session": config["oidc"]["session"],  # Non-sensitive, use config values
    }

    logger.info(f"Configuring OIDC authentication for {env_name}")

    # Get the client secret if not provided
    if not oidc_config["client_secret"]:
        oidc_config["client_secret"] = get_client_secret()

    if not oidc_config["client_secret"]:
        raise DeploymentError("OIDC client secret is required")

    # Find the HTTPS listener
    listener_arn, load_balancer_arn = find_listener_arn(env_name)
    if not listener_arn:
        raise DeploymentError("Could not find HTTPS listener")

    # Get domain from the certificate
    domain = get_domain_from_listener(listener_arn)

    # Get target group ARN
    target_group_arn = find_target_group_arn(load_balancer_arn)

    # Clean existing rules
    elbv2_client = ClientManager.get_client("elbv2")
    logger.info("Removing existing listener rules")

    rules_removed = 0
    for rule in elbv2_client.describe_rules(ListenerArn=listener_arn)["Rules"]:
        if not rule.get("IsDefault", False):
            elbv2_client.delete_rule(RuleArn=rule["RuleArn"])
            rules_removed += 1

    if rules_removed > 0:
        logger.info(f"Removed {rules_removed} rules")
    else:
        logger.info(f"No rules to remove")

    # Configure authentication action
    logger.info("Configuring OIDC authentication")

    auth_action = {
        "Type": "authenticate-oidc",
        "AuthenticateOidcConfig": {
            "Issuer": oidc_config["issuer"],
            "AuthorizationEndpoint": oidc_config["endpoints"]["authorization"],
            "TokenEndpoint": oidc_config["endpoints"]["token"],
            "UserInfoEndpoint": oidc_config["endpoints"]["userinfo"],
            "ClientId": oidc_config["client_id"],
            "ClientSecret": oidc_config["client_secret"],
            "SessionCookieName": oidc_config["session"]["cookie_name"],
            "SessionTimeout": oidc_config["session"]["timeout"],
            "Scope": oidc_config["session"]["scope"],
            "OnUnauthenticatedRequest": "authenticate",
        },
    }

    # Set default action to 503
    logger.info("Setting default listener action to deny unauthorized access")
    elbv2_client.modify_listener(
        ListenerArn=listener_arn,
        DefaultActions=[
            {
                "Type": "fixed-response",
                "FixedResponseConfig": {
                    "MessageBody": "Unauthorized Access",
                    "StatusCode": "503",
                    "ContentType": "text/plain",
                },
            }
        ],
    )

    # Create authenticated access rule
    logger.info("Creating authentication rule")
    elbv2_client.create_rule(
        ListenerArn=listener_arn,
        Priority=1,
        Conditions=[{"Field": "path-pattern", "Values": ["/*"]}],
        Actions=[
            {**auth_action, "Order": 1},
            {"Type": "forward", "TargetGroupArn": target_group_arn, "Order": 2},
        ],
    )

    # Configure HTTP to HTTPS redirect
    logger.info("Configuring HTTP to HTTPS redirect")
    http_listener = next(
        (
            l
            for l in elbv2_client.describe_listeners(LoadBalancerArn=load_balancer_arn)[
                "Listeners"
            ]
            if l["Port"] == 80
        ),
        None,
    )
    if http_listener:
        elbv2_client.modify_listener(
            ListenerArn=http_listener["ListenerArn"],
            DefaultActions=[
                {
                    "Type": "redirect",
                    "RedirectConfig": {
                        "Protocol": "HTTPS",
                        "Port": "443",
                        "StatusCode": "HTTP_301",
                    },
                }
            ],
        )
    else:
        logger.info("HTTP listener not found")

    logger.info(f"OIDC authentication successfully configured for https://{domain}")
