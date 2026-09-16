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
    return ChatBedrockConverse(
        client=bedrock_client,
        model_id=DIAGNOSTICS_MODEL_ID,
        temperature=0.0,
    )

def get_remediation_model():
    return ChatBedrockConverse(
        client=bedrock_client,
        model_id=REMEDIATION_MODEL_ID,
        temperature=0.2,
    )
