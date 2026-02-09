#!/usr/bin/env bash

# Ask AWS profile
read -p "Please enter the AWS Profile [default]? " AWS_PROFILE
AWS_PROFILE=${AWS_PROFILE:-default}

STACK_NAME=EC2-Instance-Rotation
AWS_REGION=ap-south-1   # change if you want to ask this also

# Ask bucket name
read -p "Enter the S3 bucket name for artifacts: " BUCKET_NAME

echo "Starting deployment..."
echo "Profile : $AWS_PROFILE"
echo "Region  : $AWS_REGION"
echo "Bucket  : $BUCKET_NAME"

# Build
sam build --template-file master.yaml

# Package
sam package \
  --template-file .aws-sam/build/template.yaml \
  --output-template-file temp-template.template \
  --s3-bucket $BUCKET_NAME \
  --region $AWS_REGION \
  --profile $AWS_PROFILE

# Deploy
sam deploy \
  --template-file temp-template.template \
  --disable-rollback \
  --stack-name $STACK_NAME \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND CAPABILITY_NAMED_IAM \
  --s3-bucket $BUCKET_NAME \
  --region $AWS_REGION \
  --profile $AWS_PROFILE

# Cleanup
rm -f temp-template.template

echo "✅ Deployment completed"