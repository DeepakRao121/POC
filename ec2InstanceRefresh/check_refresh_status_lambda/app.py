import boto3
from datetime import datetime
import os

# --- Configuration for DynamoDB ---
DYNAMODB_TABLE_NAME = os.environ.get('ASG_REFRESH_MONITOR_TABLE', 'ASG_Refresh_Monitor_TBD')
dynamodb_resource = boto3.resource('dynamodb')
monitor_table = dynamodb_resource.Table(DYNAMODB_TABLE_NAME)

autoscaling_client = boto3.client('autoscaling')

def lambda_handler(event, context):
    asg_name = event["asg_name"]
    refresh_id = event["refresh_id"]
    
    # Get the current iteration count from the Step Function's state input
    # The Step Function ensures this is passed from the $.status_check_counter
    current_iteration_count = event.get('status_check_counter', {}).get('check_count', 0)
    
    # Increment the count for logging/display purposes (since the Step Function increments AFTER the wait)
    db_iteration_count = current_iteration_count + 1 
    
    # 1. Check ASG Refresh Status
    try:
        resp = autoscaling_client.describe_instance_refreshes(
            AutoScalingGroupName=asg_name,
            InstanceRefreshIds=[refresh_id]
        )
        
        # Status can be 'Pending', 'InProgress', 'Successful', 'Failed', 'Cancelled'
        instance_refresh_status = resp["InstanceRefreshes"][0]["Status"]
        status_message = f"ASG refresh status: {instance_refresh_status}"
        
        # 2. Update DynamoDB Record
        monitor_table.update_item(
            Key={'asg_name': asg_name},
            UpdateExpression="SET current_status = :s, iteration_count = :i, last_update_time = :l, last_message = :m",
            ExpressionAttributeValues={
                ':s': instance_refresh_status,
                ':i': db_iteration_count,
                ':l': datetime.now().isoformat(),
                ':m': status_message
            }
        )

        # 3. Check for Step Function enforced timeout (45 checks = 45 minutes)
        max_checks = event.get('status_check_counter', {}).get('max_checks', 45)

        if instance_refresh_status in ["Pending", "InProgress"] and db_iteration_count >= max_checks:
            # We are hitting the max iteration count, force a 'TIMEOUT_FAILED' status update
            timeout_message = f"Instance Refresh timed out after {max_checks} minutes."
            
            monitor_table.update_item(
                Key={'asg_name': asg_name},
                UpdateExpression="SET current_status = :s, last_update_time = :l, last_message = :m",
                ExpressionAttributeValues={
                    ':s': 'TIMEOUT_FAILED',
                    ':l': datetime.now().isoformat(),
                    ':m': timeout_message
                }
            )
            # The Step Function's "CheckCounter" state will catch this, but we update the DB here.
            # We let the Step Function flow handle the failure state via 'MarkTimeoutFailure'.

        # 4. Return status for Step Function Choice state (EvaluateStatus)
        return {
            "status": instance_refresh_status,
            "asg_name": asg_name
        }

    except Exception as e:
        error_message = f"Error in check_refresh_status_lambda_TBD for {asg_name}: {str(e)}"
        print(error_message)
        
        # Ensure a record is written even on a critical failure during checks
        monitor_table.update_item(
            Key={'asg_name': asg_name},
            UpdateExpression="SET current_status = :s, iteration_count = :i, last_update_time = :l, last_message = :m",
            ExpressionAttributeValues={
                ':s': 'CHECK_ERROR',
                ':i': db_iteration_count,
                ':l': datetime.now().isoformat(),
                ':m': error_message
            }
        )
        raise # Re-raise the exception for the Step Function to handle