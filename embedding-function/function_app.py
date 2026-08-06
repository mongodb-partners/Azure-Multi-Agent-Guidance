"""Azure Function for Embedding Generation.

Simple REST API to generate text embeddings using Azure OpenAI.
Designed to be used as an OpenAPI tool in Azure AI Foundry agents.
"""
import azure.functions as func
import json
import logging
import os
import urllib.request

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Azure OpenAI configuration
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-ada-002")


def call_azure_openai_embedding(text: str) -> list[float]:
    """Generate embedding vector for text using Azure OpenAI."""
    url = f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{EMBEDDING_MODEL}/embeddings?api-version=2024-06-01"
    data = json.dumps({"input": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "api-key": AZURE_OPENAI_API_KEY,
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        return result["data"][0]["embedding"]


@app.route(route="embed", methods=["POST"])
def generate_embedding(req: func.HttpRequest) -> func.HttpResponse:
    """
    Generate embedding vector for text.
    
    Request body: {"text": "your text here"}
    Response: {"embedding": [0.1, 0.2, ...], "dimensions": 1536, "model": "..."}
    """
    logging.info("Embedding request received")
    
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON in request body"}),
            status_code=400,
            mimetype="application/json"
        )
    
    text = body.get("text")
    if not text:
        return func.HttpResponse(
            json.dumps({"error": "Missing required field: text"}),
            status_code=400,
            mimetype="application/json"
        )
    
    try:
        embedding = call_azure_openai_embedding(text)
        logging.info(f"Generated embedding for: {text[:50]}... (dims: {len(embedding)})")
        
        return func.HttpResponse(
            json.dumps({
                "embedding": embedding,
                "dimensions": len(embedding),
                "model": EMBEDDING_MODEL
            }),
            mimetype="application/json"
        )
    except Exception as e:
        logging.error(f"Embedding generation failed: {e}")
        return func.HttpResponse(
            json.dumps({"error": f"Embedding generation failed: {str(e)}"}),
            status_code=500,
            mimetype="application/json"
        )


@app.route(route="health", methods=["GET"])
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    """Health check endpoint."""
    return func.HttpResponse(
        json.dumps({
            "status": "healthy",
            "model": EMBEDDING_MODEL,
            "endpoint_configured": bool(AZURE_OPENAI_ENDPOINT)
        }),
        mimetype="application/json"
    )
