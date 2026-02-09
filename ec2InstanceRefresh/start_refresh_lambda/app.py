import boto3
from datetime import datetime
import os

# --- Configuration for DynamoDB ---
DYNAMODB_TABLE_NAME = os.environ.get('ASG_REFRESH_MONITOR_TABLE')
dynamodb_resource = boto3.resource('dynamodb')
monitor_table = dynamodb_resource.Table(DYNAMODB_TABLE_NAME)

autoscaling_client = boto3.client('autoscaling')
elb_client = boto3.client('elbv2') # Client for Elastic Load Balancing v2 (for Target Groups)

def check_target_group_health(asg_name):
    """
    Checks if all registered targets for the ASG's associated target groups are healthy.
    Returns True if all targets are healthy AND the count of targets is greater than zero.
    Returns False if:
        1. Any target group has at least one UNHEALTHY instance.
        2. Any associated target group has ZERO registered targets.
    """
    
    # 1. Describe the ASG to get associated Target Group ARNs
    asg_info = autoscaling_client.describe_auto_scaling_groups(
        AutoScalingGroupNames=[asg_name]
    )["AutoScalingGroups"][0]
    
    target_group_arns = asg_info.get("TargetGroupARNs", [])
    
    # If the ASG is not attached to any target group, assume health is not a block.
    if not target_group_arns:
        return True, "No associated target groups found. Proceeding."

    # 2. Iterate through each associated Target Group
    for tg_arn in target_group_arns:
        # Get the health status of all registered instances
        health_resp = elb_client.describe_target_health(
            TargetGroupArn=tg_arn
        )
        
        targets = health_resp.get("TargetHealthDescriptions", [])

        # --- CHECK: ENSURE TARGET GROUP IS NOT EMPTY ---
        if not targets:
            return False, f"Target Group {tg_arn} found with ZERO registered targets. Skipping refresh."
        
        # 3. Check health for each target in the group
        for target_health in targets:
            health_status = target_health["TargetHealth"]["State"]
            
            if health_status != "healthy":
                # We found at least one unhealthy target
                print(f"Target Group {tg_arn} has an UNHEALTHY target: {health_status} for instance {target_health['Target']['Id']}")
                return False, f"Unhealthy target found in {tg_arn} (Status: {health_status})"

    # If the loop finishes after successfully checking targets and finding at least one target in each TG.
    return True, "All registered targets across all associated target groups are present and healthy."


def lambda_handler(event, context):
    asg_name = event["asg_name"]
    
    # --- 1. Initial DynamoDB Record Setup ---
    # We write a record immediately with a preliminary status
    
    initial_status = "PRE_CHECK_FAIL" # Default to fail if checks block it
    
    try:
        asg_info = autoscaling_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[asg_name]
        )["AutoScalingGroups"][0]

        desired = asg_info["DesiredCapacity"]

        # --- 2. Check for Desired Capacity of 0 ---
        if desired == 0:
            message = f"Skipped {asg_name}: DesiredCapacity is 0."
            print(message)
            initial_status = "SKIPPED_CAPACITY_ZERO"
            
            monitor_table.put_item(
                Item={
                    'asg_name': asg_name,
                    'current_status': initial_status,
                    'start_time': datetime.now().isoformat(),
                    'last_update_time': datetime.now().isoformat(),
                    'refresh_id': 'N/A',
                    'iteration_count': 0,
                    'last_message': message
                }
            )
            return {
                "asg_name": asg_name,
                "skip": True,
                "message": message
            }

        # --- 3. Check for Target Group Health (and Target Count) ---
        is_healthy, health_message = check_target_group_health(asg_name)

        if not is_healthy:
            message = f"Skipped {asg_name}: Target Health Check Failed ({health_message})."
            print(message)
            initial_status = "SKIPPED_UNHEALTHY"
            
            monitor_table.put_item(
                Item={
                    'asg_name': asg_name,
                    'current_status': initial_status,
                    'start_time': datetime.now().isoformat(),
                    'last_update_time': datetime.now().isoformat(),
                    'refresh_id': 'N/A',
                    'iteration_count': 0,
                    'last_message': message
                }
            )
            return {
                "asg_name": asg_name,
                "skip": True,
                "message": message
            }

        # --- 4. Proceed with Instance Refresh ---
        message = f"Starting instance refresh for {asg_name}. {health_message}"
        print(message)
        
        resp = autoscaling_client.start_instance_refresh(
            AutoScalingGroupName=asg_name,
            Preferences={
                "MinHealthyPercentage": 100,
                "MaxHealthyPercentage": 200,
                "InstanceWarmup": 10,
                "SkipMatching": False,
                "ScaleInProtectedInstances": "Refresh"
            }
        )
        refresh_id = resp["InstanceRefreshId"]
        
        # --- 5. Record Start of Refresh in DynamoDB ---
        initial_status = "REFRESH_STARTED"
        monitor_table.put_item(
            Item={
                'asg_name': asg_name,
                'current_status': initial_status,
                'start_time': datetime.now().isoformat(),
                'last_update_time': datetime.now().isoformat(),
                'refresh_id': refresh_id,
                'iteration_count': 0,
                'last_message': message
            }
        )
        
        return {
            "asg_name": asg_name,
            "skip": False,
            "refresh_id": refresh_id,
            "message": f"Instance refresh started successfully for {asg_name}."
        }

    except Exception as e:
        error_message = f"Error in start_refresh_lambda_TBD for {asg_name}: {str(e)}"
        print(error_message)
        
        # Ensure a record is written even on a critical failure
        monitor_table.put_item(
            Item={
                'asg_name': asg_name,
                'current_status': 'CRITICAL_ERROR',
                'start_time': datetime.now().isoformat(),
                'last_update_time': datetime.now().isoformat(),
                'refresh_id': 'N/A',
                'iteration_count': 0,
                'last_message': error_message
            }
        )
        raise # Re-raise the exception to be caught by the Step Function's Catch block