#!/usr/bin/env bash

read -p "Please enter the AWS Profile [default]? " AWS_PROFILE
AWS_PROFILE=${AWS_PROFILE:-default}

if [[ $AWS_PROFILE != "" ]]; then
  PROFILE_CHECK=$([[ $(aws configure --profile $AWS_PROFILE list) && $? -eq 0 ]] && echo 1 || echo 0)
else
  PROFILE_CHECK=0
fi

if [[ $PROFILE_CHECK != 1 ]]; then
  echo "Switching to \"default\""
  AWS_PROFILE=default
fi

STACK_NAME=EC2-Instance-Rotation

PS3='Please enter the environment (enter corresponding number): '
options=("dev" "qa" "qa2" "uat" "int" "int2" "production")
select opt in "${options[@]}"; do
  case $opt in
  "dev" | "qa" | "uat" | "qa2" | "int2")
    AWS_REGION=ap-south-1
    BUCKET_NAME="cfn-templates-v2-ap-south-1-$opt"
    ENV=$opt
    break
    ;;
  "int")
    AWS_REGION=us-east-1
    BUCKET_NAME="cfn-templates-v2-$opt"
    ENV=$opt
    break
    ;;    
  "production")
    AWS_REGION=ap-south-1
    BUCKET_NAME=cfn-oneaboveall-templates-production
    ENV=$opt
    break
    ;;
  *) echo "invalid option" ;;
  esac
done

echo "Starting Deployment on $ENV"

sam build --template-file master.yaml

sam package \
  --template-file .aws-sam/build/template.yaml \
  --output-template-file temp-template.template \
  --s3-bucket $BUCKET_NAME \
  --region $AWS_REGION \
  --profile $AWS_PROFILE

sam deploy \
  --template-file temp-template.template \
  --disable-rollback \
  --stack-name $STACK_NAME \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND CAPABILITY_NAMED_IAM \
  --s3-bucket $BUCKET_NAME \
  --region $AWS_REGION \
  --profile $AWS_PROFILE \
  --parameter-overrides \
  EnvironmentType="$ENV"

# Clean Up
rm -f temp-template.template