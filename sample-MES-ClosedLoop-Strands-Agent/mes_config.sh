#!/bin/bash

# mes_config.sh - Configuration script for MES Agent Manager
# Usage: source mes_config.sh

echo "Setting up MES Agent Manager environment variables..."

# =============================================================================
# DATABASE CONFIGURATION
# =============================================================================
#export MES_DB_PATH="/path/to/your/mes.db"
#echo "✓ Database path set to: \$MES_DB_PATH"

# =============================================================================
# AWS BEDROCK MODEL CONFIGURATION
# =============================================================================
export MES_MODEL_ID="apac.anthropic.claude-3-7-sonnet-20250219-v1:0"
export AWS_REGION="ap-south-1"
echo "✓ Model ID set to: \$MES_MODEL_ID"
echo "✓ AWS Region set to: \$AWS_REGION"

# =============================================================================
# EMAIL CONFIGURATION
# =============================================================================
export MES_SENDER_EMAIL="operations.team@yourcompany.com"
export MES_RECIPIENT_EMAIL="operations.team@yourcompany.com"
export MES_BASE_URL="https://dfxyzl4n.cloudfront.net/proxy/8501"
echo "✓ Sender email set to: \$MES_SENDER_EMAIL"
echo "✓ Recipient email set to: \$MES_RECIPIENT_EMAIL"
echo "✓ Base URL set to: \$MES_BASE_URL"

# =============================================================================
# RETRY CONFIGURATION
# =============================================================================
export MES_MAX_RETRY_ATTEMPTS="10"
export MES_RETRY_MODE="standard"
echo "✓ Max retry attempts set to: \$MES_MAX_RETRY_ATTEMPTS"
echo "✓ Retry mode set to: \$MES_RETRY_MODE"

# =============================================================================
# AWS CREDENTIALS (if not using IAM roles)
# =============================================================================
# Uncomment and set these if you're not using IAM roles
# export AWS_ACCESS_KEY_ID="your-access-key-id"
# export AWS_SECRET_ACCESS_KEY="your-secret-access-key"
# export AWS_SESSION_TOKEN="your-session-token"  # Only if using temporary credentials

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================
export MES_LOG_LEVEL="INFO"
export MES_LOG_FORMAT="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
echo "✓ Log level set to: \$MES_LOG_LEVEL"