#!/bin/bash
set -e

echo "Building Lambda packages..."

# Create a temporary directory for the layer
mkdir -p layer_build/python/lib/python3.11/site-packages

# Install dependencies for the Lambda layer
pip install -r lambda_functions/requirements.txt -t layer_build/python/lib/python3.11/site-packages

# Create the layer zip
cd layer_build
zip -r ../lambda_functions/layer.zip python/
cd ..

# Create authorizer and initial Lambda zip files (single file functions)
for lambda in authorizer initial_lambda; do
    cd lambda_functions
    zip -j ${lambda}.zip ${lambda}.py
    cd ..
done

# Create processing Lambda zip (Python package with multiple modules)
echo "Packaging processing Lambda (Python package)..."
cd lambda_functions
# Create zip with the entire processing package directory
zip -r processing_lambda.zip processing/
cd ..

echo "Build complete!"
echo ""
echo "Files created:"
ls -lh lambda_functions/*.zip
echo ""
echo "Next steps:"
echo "1. Set your secrets:"
echo "   export TF_VAR_openai_api_key='your-openai-key'"
echo "   export TF_VAR_slack_signing_secret='your-slack-signing-secret'"
echo "   export TF_VAR_slack_bot_token='your-slack-bot-token'"
echo ""
echo "2. Initialize and apply Terraform:"
echo "   cd terraform"
echo "   terraform init"
echo "   terraform plan"
echo "   terraform apply"
echo ""
echo "3. Configure Slack with the API Gateway URL from terraform output"
