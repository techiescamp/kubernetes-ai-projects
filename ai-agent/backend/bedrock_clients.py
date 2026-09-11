import os
import boto3
from dotenv import load_dotenv
from langchain_aws import ChatBedrock, ChatBedrockConverse

load_dotenv()

# Resolve AWS Region and model IDs from the environment (see .env.example)
AWS_REGION = os.environ["AWS_REGION"]
DIAGNOSTICS_MODEL_ID = os.environ["DIAGNOSTICS_MODEL_ID"]
REMEDIATION_MODEL_ID = os.environ["REMEDIATION_MODEL_ID"]

# Create a boto3 Session to automatically resolve credentials from environment variables,
# IAM roles, or ~/.aws/credentials profiles.
session = boto3.Session()
bedrock_client = session.client(
    service_name="bedrock-runtime",
    region_name=AWS_REGION
)

def get_diagnostics_model():
    """
    Returns ChatBedrock instance for the diagnostics model using resolved boto3 credentials.
    """
    return ChatBedrock(
        client=bedrock_client,
        model_id=DIAGNOSTICS_MODEL_ID,
        model_kwargs={"temperature": 0.0}
    )

def get_remediation_model():
    """
    Returns ChatBedrockConverse instance for the remediation model using resolved boto3
    credentials. Uses the native Bedrock Converse API explicitly (not the legacy ChatBedrock
    invoke_model path) for reliable tool calling regardless of which model REMEDIATION_MODEL_ID
    points at. Defaults to the same Nova Pro model as diagnostics - Llama4 Maverick was tried here
    originally but Bedrock rejected it for this AWS account's country/region ("Access to Meta
    Llama models is not allowed..."), confirmed via a real deployed test - see SPEC.md.
    """
    return ChatBedrockConverse(
        client=bedrock_client,
        model_id=REMEDIATION_MODEL_ID,
        temperature=0.2,
    )

