#!/bin/bash

# EMERGENCY FIX SCRIPT for API Gateway Deployment Issue
# Run this from the terraform/ directory

set -e

echo "=========================================="
echo "EMERGENCY FIX: API Gateway Deployment"
echo "=========================================="
echo ""

# Step 1: Backup current state
echo "Step 1: Backing up local state files..."
if [ -f terraform.tfstate ]; then
    cp terraform.tfstate terraform.tfstate.backup.$(date +%s)
    echo "✓ Local state backed up"
fi

if [ -f terraform.tfstate.backup ]; then
    echo "✓ Found backup state file"
    echo ""
    echo "⚠ WARNING: Remote state backend has been temporarily disabled"
    echo "  The backup file will be used for current operations"
    echo ""
fi

# Step 2: Re-initialize terraform without backend
echo "Step 2: Re-initializing Terraform (local backend)..."
terraform init -reconfigure

# Step 3: Check current state
echo ""
echo "Step 3: Checking current state..."
echo "Current resources:"
terraform state list 2>/dev/null || echo "  (State is empty or unavailable)"

echo ""
echo "=========================================="
echo "NEXT STEPS - Choose ONE option:"
echo "=========================================="
echo ""
echo "OPTION 1: MANUAL AWS CONSOLE FIX (Easiest)"
echo "-------------------------------------------"
echo "1. Go to AWS Console → API Gateway → Your API (ff-moogle-bot-api)"
echo "2. Click 'Stages' in left sidebar"
echo "3. Delete the 'prod' stage (this will remove the deployment reference)"
echo "4. Run: terraform apply"
echo ""
echo "OPTION 2: TERRAFORM STATE MANIPULATION"
echo "--------------------------------------"
echo "Run these commands:"
echo "  terraform state rm aws_api_gateway_stage.prod 2>/dev/null || true"
echo "  terraform state rm aws_api_gateway_deployment.prod 2>/dev/null || true"  
echo "  terraform apply"
echo ""
echo "OPTION 3: DESTROY AND RECREATE"
echo "------------------------------"
echo "⚠ WARNING: This will delete the API Gateway and recreate it!"
echo "  terraform destroy -target aws_api_gateway_stage.prod -target aws_api_gateway_deployment.prod"
echo "  terraform apply"
echo ""
echo "=========================================="
echo "After fixing, re-enable remote state:"
echo "=========================================="
echo "1. Uncomment the backend block in main.tf"
echo "2. Run: terraform init -migrate-state"
echo ""
