import os
import boto3
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse

load_dotenv()

AWS_REGION = os.environ["AWS_REGION"]
DIAGNOSTICS_MODEL_ID = os.environ["DIAGNOSTICS_MODEL_ID"]
REMEDIATION_MODEL_ID = os.environ["REMEDIATION_MODEL_ID"]

session = boto3.Session()
bedrock_client = session.client(
    service_name="bedrock-runtime",
    region_name=AWS_REGION
)

def get_diagnostics_model():
    """
    Returns a ChatBedrockConverse instance for the diagnostics model using resolved boto3
    credentials. Uses the native Bedrock Converse API - diagnose_node and verify_remediation_node
    both call .bind_tools() on this (24 read tools), and Converse is what gives reliable tool
    calling regardless of which model DIAGNOSTICS_MODEL_ID points at (same reasoning as
    get_remediation_model below; this used to be the older ChatBedrock/invoke_model wrapper, which
    had no documented reason to differ from remediation on this point).
    """
    return ChatBedrockConverse(
        client=bedrock_client,
        model_id=DIAGNOSTICS_MODEL_ID,
        temperature=0.0,
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

