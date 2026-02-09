import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def clear_dynamodb_table(table_name):
    dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')
    table = dynamodb.Table(table_name)
    
    # 1. Get the Key Schema of the table dynamically
    # This tells us exactly what the Partition Key and Sort Key are named
    key_names = [k['AttributeName'] for k in table.key_schema]
    logger.info(f"Table keys detected: {key_names}")

    # 2. Scan the table
    scan = table.scan()
    items = scan.get('Items', [])
    
    if not items:
        logger.info("Table is already empty.")
        return

    # 3. Delete items using the correct schema
    with table.batch_writer() as batch:
        for item in items:
            # Create a key dictionary containing only the required Key attributes
            key_to_delete = {k: item[k] for k in key_names}
            batch.delete_item(Key=key_to_delete)
            
    logger.info(f"Successfully deleted {len(items)} items.")

def lambda_handler(event, context):
    # 1. Clear the DynamoDB table first
    table_name = os.environ.get('DYNAMODB_TABLE_NAME')
    clear_dynamodb_table(table_name)

    # 2. Proceed with listing running instances
    ec2 = boto3.client('ec2', region_name='ap-south-1')
    paginator = ec2.get_paginator('describe_instances')
    
    filters = [{'Name': 'instance-state-name', 'Values': ['running']}]
    instances_to_check = []
    
    page_iterator = paginator.paginate(Filters=filters)
    
    for page in page_iterator:
        for reservation in page['Reservations']:
            for ins in reservation['Instances']:
                private_ip = ins.get('PrivateIpAddress')
                if not private_ip:
                    continue
                
                instance_name = "Unknown"
                if 'Tags' in ins:
                    instance_name = next((tag['Value'] for tag in ins['Tags'] if tag['Key'] == 'Name'), "Unknown")
                
                instances_to_check.append({
                    "instance_id": ins['InstanceId'],
                    "instance_ip": private_ip,
                    "instance_name": instance_name,
                    "key_pair_name": ins.get('KeyName'),
                    "state": ins['State']['Name']
                })
    
    logger.info(f"Discovery complete. Found {len(instances_to_check)} running instances.")
    return instances_to_check