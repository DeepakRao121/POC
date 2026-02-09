import paramiko
import io
import boto3
import logging
import time
import os
from datetime import datetime
from datetime import timedelta
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    ec2 = boto3.client('ec2', region_name='ap-south-1')
    dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
    table = dynamodb.Table('InstanceAduditStatusChecksTBD')
    
    # --- CONFIGURATION FROM STEP FUNCTION ---
    instance_id = event.get('instance_id')
    instance_ip = event.get('instance_ip')
    instance_name = event.get('instance_name', 'Unknown')
    key_pair_name = event.get('key_pair_name')
    
    if not instance_id or not instance_ip:
        raise Exception("Input missing instance_id or instance_ip")
    
    temp_sg_id = None
    original_sg_ids = []
    # timestamp = datetime.now().isoformat()

    now_utc = datetime.utcnow()
    now_ist = now_utc + timedelta(hours=5, minutes=30)
    timestamp = now_ist.strftime('%Y-%m-%d %H:%M:%S')

    # 1. Get the Lambda's own Security Group dynamically
    lambda_client = boto3.client('lambda')
    try:
        response = lambda_client.get_function_configuration(FunctionName=context.function_name)
        lambda_sg_id = response['VpcConfig']['SecurityGroupIds'][0]
    except (KeyError, IndexError):
        logger.error("Lambda is not configured with a VPC.")
        raise Exception("Lambda must be in a VPC to perform this task.")

    try:
        # 2. Pre-flight check: Verify instance state
        instance_desc = ec2.describe_instances(InstanceIds=[instance_id])
        instance_data = instance_desc['Reservations'][0]['Instances'][0]

        current_state = instance_data['State']['Name']
        if current_state != 'running':
            logger.warning(f"Instance {instance_id} is {current_state}. Skipping.")
            table.put_item(Item={
                'InstanceId': instance_id,
                'Timestamp': timestamp,
                'InstanceName': instance_name,
                'OverallHealth': f'SKIPPED ({current_state.upper()})'
            })
            return {"statusCode": 200, "message": "Instance not running"}

        vpc_id = instance_data['VpcId']
        original_sg_ids = [sg['GroupId'] for sg in instance_data['SecurityGroups']]

        # 3. JIT Networking Setup
        sg_name = f"Temp-SSH-{instance_id}"
        try:
            sg_response = ec2.create_security_group(
                GroupName=sg_name, Description=f"JIT Access for {instance_name}", VpcId=vpc_id
            )
            temp_sg_id = sg_response['GroupId']
        except ClientError as e:
            if e.response['Error']['Code'] == 'InvalidGroup.Duplicate':
                existing = ec2.describe_security_groups(Filters=[{'Name': 'group-name', 'Values': [sg_name]}])
                temp_sg_id = existing['SecurityGroups'][0]['GroupId']
            else:
                raise e

        ec2.authorize_security_group_ingress(
            GroupId=temp_sg_id,
            IpPermissions=[{'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22,
                            'UserIdGroupPairs': [{'GroupId': lambda_sg_id}]}]
        )

        ec2.modify_instance_attribute(InstanceId=instance_id, Groups=original_sg_ids + [temp_sg_id])
        logger.info(f"SG {temp_sg_id} attached. Waiting 15s...")
        time.sleep(15)

        # --- SSH AND SECRET LOGIC (Protected Block) ---
        try:
            if not key_pair_name:
                raise ValueError("No KeyPair associated with this instance")

            # Fetch Secret with proper error mapping
            s_client = boto3.client('secretsmanager', region_name='ap-south-1')
            try:
                secret_response = s_client.get_secret_value(SecretId=key_pair_name)
                secret_string = secret_response['SecretString']
            except ClientError as e:
                err_code = e.response['Error']['Code']
                if err_code == 'ResourceNotFoundException':
                    raise Exception(f"Secret '{key_pair_name}' missing in Secrets Manager")
                elif err_code == 'AccessDeniedException':
                    raise Exception(f"Lambda IAM role denied access to secret '{key_pair_name}'")
                else:
                    raise Exception(f"SecretsManager Error: {err_code}")

            # Connect SSH

            # ... (Secret fetching code remains the same) ...

            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(secret_string))
            
            # List of common AWS usernames to try
            usernames = ["ubuntu", "ec2-user"]
            connected = False
            last_error = ""

            for user in usernames:
                try:
                    logger.info(f"Attempting SSH as {user} for {instance_ip}...")
                    ssh_client.connect(
                        hostname=instance_ip, 
                        username=user, 
                        pkey=pkey, 
                        timeout=10,
                        allow_agent=False,
                        look_for_keys=False
                    )
                    connected = True
                    logger.info(f"Successfully connected as {user}")
                    break
                except paramiko.AuthenticationException:
                    last_error = f"Auth failed for {user}"
                    continue
                except Exception as e:
                    last_error = str(e)
                    continue

            if not connected:
                raise Exception(f"Could not connect with any known username. Last error: {last_error}")

            time.sleep(2) 

            # Run Checks (Uses Exit Codes for 100% accuracy)
            services = ["amazon-cloudwatch-agent", "auditd"]
            service_statuses = {}
            
            for service in services:
                # 'systemctl is-active' is the standard for scripting
                # It returns exit code 0 if active, and non-zero if inactive or missing
                stdin, stdout, stderr = ssh_client.exec_command(f"systemctl is-active {service}")
                
                # We check the exit status rather than searching the text string
                exit_status = stdout.channel.recv_exit_status()
                
                if exit_status == 0:
                    service_statuses[service] = "active"
                else:
                    # If not found or inactive, we log as inactive
                    service_statuses[service] = "inactive"
                    logger.info(f"Service {service} on {instance_id} returned exit code {exit_status}")
            
            ssh_client.close()

            # --- NEW FEATURE: CloudWatch Log Verification (Dynamic & 10hr Window) ---
            audit_streaming = "NO"
            try:
                # 1. Use dynamic Region and Log Group
                current_region = os.environ.get('AWS_REGION', 'ap-south-1')
                logs_client = boto3.client('logs', region_name=current_region)
                
                # 2. Construct Dynamic Stream Name based on Instance Name and ID
                # Format: /{instance_name}/{instance_id}
                stream_prefix = f"/{instance_name}/{instance_id}"
                
                logger.info(f"Checking CloudWatch Logs for stream: {stream_prefix}")
                
                log_response = logs_client.describe_log_streams(
                    logGroupName='/ec2/auditd',
                    logStreamNamePrefix=stream_prefix,
                    limit=1
                )
                
                if log_response.get('logStreams'):
                    # Pick the specific stream that matches exactly
                    stream = log_response['logStreams'][0]
                    last_event = stream.get('lastEventTimestamp')
                    
                    if last_event:
                        # Convert 10 hours to milliseconds
                        current_time_ms = int(time.time() * 1000)
                        ten_hours_ms = 10 * 60 * 60 * 1000
                        # ten_hours_ms = 100
                        
                        if (current_time_ms - last_event) < ten_hours_ms:
                            audit_streaming = "YES"
                        else:
                            # Convert last_event to readable IST for the report
                            last_event_dt = datetime.utcfromtimestamp(last_event/1000) + timedelta(hours=5, minutes=30)
                            stale_time = last_event_dt.strftime('%H:%M')
                            audit_streaming = f"STALE (Last seen at {stale_time})"
                    else:
                        audit_streaming = "NO (Stream empty)"
                else:
                    audit_streaming = f"NO (Stream {stream_prefix} not found)"
                    
            except Exception as log_err:
                logger.error(f"Failed to check CW Logs: {str(log_err)}")
                audit_streaming = "ERROR"

            # Final success record
            item = {
                'InstanceId': instance_id,
                'Timestamp': timestamp,
                'InstanceName': instance_name,
                'CloudWatchAgentStatus': service_statuses.get('amazon-cloudwatch-agent'),
                'AuditdStatus': service_statuses.get('auditd'),
                'AuditStreaming': audit_streaming,
                'OverallHealth': 'HEALTHY' if (all(s == 'active' for s in service_statuses.values()) and audit_streaming == "YES") else 'UNHEALTHY'
            }
            table.put_item(Item=item)
            return {"statusCode": 200, "data": item}

        except Exception as inner_err:
            # Catch SSH/Secret errors, log them, but don't crash (keep cleanup running)
            logger.error(f"Audit failed for {instance_id}: {str(inner_err)}")
            table.put_item(Item={
                'InstanceId': instance_id,
                'Timestamp': timestamp,
                'InstanceName': instance_name,
                'OverallHealth': f"FAILED: {str(inner_err)}"
            })
            return {"status": "SKIPPED", "reason": str(inner_err)}

    except Exception as e:
        logger.error(f"Critical System Error: {str(e)}")
        raise e

    finally:
        # Ensure cleanup happens even if SSH fails
        if temp_sg_id:
            try:
                logger.info(f"Detaching and deleting SG {temp_sg_id}")
                ec2.modify_instance_attribute(InstanceId=instance_id, Groups=original_sg_ids)
                time.sleep(2)
                ec2.delete_security_group(GroupId=temp_sg_id)
            except Exception as cleanup_err:
                logger.error(f"Cleanup failed: {str(cleanup_err)}")