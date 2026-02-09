import boto3
import yaml 
import os 

s3_client = boto3.client('s3')

def lambda_handler(event, context):
    
    # -----------------------------------------------------------
    # 1. Retrieve configuration values from Environment Variables
    # -----------------------------------------------------------
    
    # Required S3 path
    BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
    KEY_NAME = os.environ.get("S3_KEY_NAME")
    
    # ASG suffix and batch control
    ASG_SUFFIX = os.environ.get("ASG_NAME_SUFFIX")
    
    # New environment variable for the target environment key
    TARGET_ENV_KEY = os.environ.get("TARGET_ENVIRONMENT") 
    
    # Convert batch_size from string (env var) to integer
    try:
        batch_size = int(os.environ.get("BATCH_SIZE", "10"))
    except ValueError:
        print("WARNING: BATCH_SIZE environment variable is not a valid integer. Defaulting to 10.")
        batch_size = 10
    
    # 2. Read the YAML file from S3
    try:
        print(f"Attempting to read s3://{BUCKET_NAME}/{KEY_NAME}")
        s3_object = s3_client.get_object(Bucket=BUCKET_NAME, Key=KEY_NAME)
        yaml_content = s3_object['Body'].read().decode('utf-8')
    except Exception as e:
        print(f"ERROR: Could not read S3 file. {e}")
        return {
            "total_asgs": 0,
            "batch_size": batch_size,
            "asg_batches": [],
            "error_detail": f"Failed to read S3 file: {str(e)}"
        }

    # 3. Parse the YAML content
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        print(f"ERROR: Could not parse YAML content. {e}")
        return {
            "total_asgs": 0,
            "batch_size": batch_size,
            "asg_batches": [],
            "error_detail": "YAML Parsing Error"
        }

    asgs_to_refresh = []
    
    # Logging the target key for clarity in CloudWatch logs
    print(f"Targeting services where key '{TARGET_ENV_KEY}' is set to 'True'.")

    # 4. Iterate, Filter, and Construct ASG Names
    service_deployment = data.get('ServiceDeployment', {})
    
    for service_name, environments in service_deployment.items():
        # environments is expected to be a dict like {'dev': 'False', 'qa': 'True', ...}
        
        if isinstance(environments, dict):
            
            # --- THE KEY CHANGE: Use the generic variable for lookup ---
            env_value_to_check = environments.get(TARGET_ENV_KEY)
            
            # Check if the value is present and is explicitly set to 'True'
            if env_value_to_check is not None and str(env_value_to_check).strip() == 'True':
                
                # Construct the ASG name with the suffix
                asg_name = f"{service_name}{ASG_SUFFIX}"
                asgs_to_refresh.append(asg_name)

    # 5. Define batch size and create batches
    
    if batch_size <= 0:
        batch_size = 10
        
    batches = [asgs_to_refresh[i:i + batch_size] 
               for i in range(0, len(asgs_to_refresh), batch_size)]

    print(f"Found {len(asgs_to_refresh)} ASGs matching the '{TARGET_ENV_KEY}: True' pattern.")
    print(f"Created {len(batches)} batches of size {batch_size}.")

    return {
        "total_asgs": len(asgs_to_refresh),
        "batch_size": batch_size,
        "asg_batches": batches
    }