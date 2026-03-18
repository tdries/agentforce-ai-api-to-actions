"""agentforce-ai-api-to-actions: Register any external API as a Salesforce Agentforce action.

Usage:
    pip install -r requirements.txt
    uvicorn main:app --reload
    open http://localhost:8000
"""

import asyncio
import base64
import io
import json
import os
import re
import time
import zipfile
from typing import AsyncGenerator, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xe

import anthropic
import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(override=False)  # don't overwrite vars already set in the shell

app = FastAPI(title="agentforce-ai-api-to-actions")
if os.path.isdir("brand"):
    app.mount("/brand", StaticFiles(directory="brand"), name="brand")
SF_VERSION = os.getenv("SF_API_VERSION", "61.0")


# ── Models ────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    url: str
    service_name: Optional[str] = None
    sf_instance_url: Optional[str] = None   # e.g. https://myorg.my.salesforce.com
    sf_username: Optional[str] = None
    sf_password: Optional[str] = None
    sf_security_token: Optional[str] = None


# ── SSE helpers ───────────────────────────────────────────────────────────────

def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Salesforce login (SOAP partner API) ───────────────────────────────────────

def sf_login(instance_url: str, username: str, password: str, security_token: str) -> tuple[str, str]:
    """Returns (session_id, sf_instance_hostname)."""
    # Normalize: ensure scheme is present
    if instance_url and not instance_url.startswith("http"):
        instance_url = "https://" + instance_url.lstrip("/")
    instance_url = (instance_url or "").rstrip("/")

    if instance_url and instance_url not in ("https://login.salesforce.com", "https://test.salesforce.com"):
        login_ep = f"{instance_url}/services/Soap/u/{SF_VERSION}"
    elif "test" in instance_url:
        login_ep = f"https://test.salesforce.com/services/Soap/u/{SF_VERSION}"
    else:
        login_ep = f"https://login.salesforce.com/services/Soap/u/{SF_VERSION}"

    soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:urn="urn:partner.soap.sforce.com">
  <soapenv:Body>
    <urn:login>
      <urn:username>{xe(username)}</urn:username>
      <urn:password>{xe(password + security_token)}</urn:password>
    </urn:login>
  </soapenv:Body>
</soapenv:Envelope>"""

    resp = requests.post(
        login_ep,
        data=soap.encode(),
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "login"},
        timeout=30,
    )

    root = ET.fromstring(resp.text)

    # SOAP faults: faultstring has NO namespace in standard SOAP
    fault = root.find(".//faultstring")
    if fault is not None:
        raise Exception(f"Salesforce login failed: {fault.text}")

    sf_ns = "urn:partner.soap.sforce.com"
    session_el = root.find(f".//{{{sf_ns}}}sessionId")
    server_el = root.find(f".//{{{sf_ns}}}serverUrl")

    if session_el is None:
        # Dump response to help debug
        snippet = re.sub(r"<[^>]+>", " ", resp.text)[:300].strip()
        raise Exception(f"Login response missing sessionId. Response: {snippet}")

    sf_instance = urlparse(server_el.text).netloc
    return session_el.text, sf_instance


# ── Fetch API docs ────────────────────────────────────────────────────────────

def fetch_docs(url: str) -> tuple[str, str]:
    """Returns (content, content_type) where content_type is 'spec' or 'html'."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AutoAPI/1.0)"}
    resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    ct = resp.headers.get("Content-Type", "")
    text = resp.text

    # Already an OpenAPI spec?
    is_yaml_url = url.lower().endswith((".yaml", ".yml"))
    is_json_url = url.lower().endswith(".json")
    is_yaml_ct = "yaml" in ct
    is_json_ct = "json" in ct

    if is_yaml_url or is_yaml_ct:
        return text, "spec"
    if (is_json_url or is_json_ct) and any(k in text[:500] for k in ('"openapi"', '"swagger"', 'openapi:', 'swagger:')):
        return text, "spec"

    # HTML — strip boilerplate and return text
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)[:80_000], "html"


# ── Generate OpenAPI spec via Claude ─────────────────────────────────────────

def generate_spec(content: str, content_type: str, service_name: str) -> tuple[dict, str]:
    """Returns (spec_dict, spec_yaml_string)."""
    client = anthropic.Anthropic()

    if content_type == "spec":
        prompt = f"""Convert this API specification to a valid OpenAPI 3.0 YAML that is compatible with Salesforce External Services.

Rules:
- Output ONLY valid YAML, no markdown fences, no explanation
- OpenAPI version must be 3.0.x
- Each operation must have a unique operationId (camelCase, letters/digits/underscores only)
- Max 20 operations (keep the most useful ones)
- Add x-sfdc-ag-action-description to each operation: 1-2 sentences for an AI agent describing when to use it
- All $ref references must resolve within the document
- Response schemas must be defined

Input spec:
{content[:60_000]}"""
    else:
        prompt = f"""Analyze this API documentation and create a complete OpenAPI 3.0 specification.

Service name: {service_name}

Rules:
- Output ONLY valid YAML (OpenAPI 3.0), no markdown fences, no explanation
- info.title: "{service_name}"
- info.version: "1.0.0"
- Extract the correct server URL from the docs
- Each operation needs: operationId (camelCase), summary, description, parameters, responses
- Add x-sfdc-ag-action-description to each operation: 1-2 sentences for an AI agent describing when to use this action
- Include request/response schemas
- Max 20 operations
- Use NoAuthentication assumptions (API key / auth handled separately in Salesforce)

API Documentation:
{content}"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```ya?ml\s*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    spec = yaml.safe_load(raw)
    return spec, raw


# ── Metadata helpers ──────────────────────────────────────────────────────────

def safe_name(name: str, max_len: int = 40) -> str:
    n = re.sub(r"[^a-zA-Z0-9]", "_", name)
    n = re.sub(r"_+", "_", n).strip("_")
    if n and n[0].isdigit():
        n = "Api_" + n
    return n[:max_len] or "ExternalAPI"


def spec_metadata(spec: dict) -> dict:
    servers = spec.get("servers") or [{}]
    server_url = servers[0].get("url", "https://api.example.com")
    # Resolve template variables to empty
    server_url = re.sub(r"\{[^}]+\}", "", server_url).rstrip("/")

    parsed = urlparse(server_url)
    host = parsed.netloc or server_url.split("/")[0]
    base_path = parsed.path or "/"
    scheme = (parsed.scheme or "https").upper()

    operations = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method.lower() in ("get", "post", "put", "patch", "delete") and isinstance(op, dict):
                op_id = op.get("operationId")
                if op_id:
                    operations.append(safe_name(op_id, 60))

    return {
        "host": host,
        "base_path": base_path,
        "scheme": scheme,
        "operations": operations[:20],
        "title": (spec.get("info") or {}).get("title", "API Service"),
        "description": ((spec.get("info") or {}).get("description") or "")[:255],
    }


def build_zip(svc_name: str, nc_name: str, spec_yaml: str, meta: dict) -> bytes:
    ops_xml = "\n".join(
        f"    <operations>\n        <active>true</active>\n        <name>{op}</name>\n    </operations>"
        for op in meta["operations"]
    )

    service_binding = json.dumps({
        "host": meta["host"],
        "basePath": meta["base_path"],
        "allowedSchemes": [meta["scheme"]],
        "requestMediaTypes": ["application/json"],
        "responseMediaTypes": ["application/json"],
    })

    label = meta["title"][:40]
    desc = meta["description"] or label

    esr_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ExternalServiceRegistration xmlns="http://soap.sforce.com/2006/04/metadata">
    <description>{xe(desc)}</description>
    <label>{xe(label)}</label>
    <namedCredential>{nc_name}</namedCredential>
{ops_xml}
    <registrationProviderType>Custom</registrationProviderType>
    <schema>{xe(spec_yaml)}</schema>
    <schemaType>OpenApi3_0</schemaType>
    <schemaUploadFileExtension>yaml</schemaUploadFileExtension>
    <schemaUploadFileName>{svc_name}</schemaUploadFileName>
    <serviceBinding>{xe(service_binding)}</serviceBinding>
    <status>Complete</status>
    <systemVersion>3</systemVersion>
</ExternalServiceRegistration>"""

    nc_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<NamedCredential xmlns="http://soap.sforce.com/2006/04/metadata">
    <allowMergeFieldsInBody>false</allowMergeFieldsInBody>
    <allowMergeFieldsInHeader>false</allowMergeFieldsInHeader>
    <endpoint>https://{xe(meta["host"])}</endpoint>
    <generateAuthorizationHeader>false</generateAuthorizationHeader>
    <label>{xe(label)}</label>
    <principalType>Anonymous</principalType>
    <protocol>NoAuthentication</protocol>
</NamedCredential>"""

    package_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>{svc_name}</members>
        <name>ExternalServiceRegistration</name>
    </types>
    <types>
        <members>{nc_name}</members>
        <name>NamedCredential</name>
    </types>
    <version>{SF_VERSION}</version>
</Package>"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("package.xml", package_xml)
        zf.writestr(f"externalServiceRegistrations/{svc_name}.externalServiceRegistration-meta.xml", esr_xml)
        zf.writestr(f"externalServiceRegistrations/{svc_name}.yaml", spec_yaml)
        zf.writestr(f"namedCredentials/{nc_name}.namedCredential-meta.xml", nc_xml)
    return buf.getvalue()


# ── Salesforce Metadata API deploy ────────────────────────────────────────────

def sf_deploy(session_id: str, instance: str, zip_bytes: bytes) -> str:
    """Kick off a deploy. Returns async ID."""
    zip_b64 = base64.b64encode(zip_bytes).decode()
    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:CallOptions><met:client>AutoAPI</met:client></met:CallOptions>
    <met:SessionHeader><met:sessionId>{session_id}</met:sessionId></met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:deploy>
      <met:ZipFile>{zip_b64}</met:ZipFile>
      <met:DeployOptions>
        <met:allowMissingFiles>false</met:allowMissingFiles>
        <met:autoUpdatePackage>false</met:autoUpdatePackage>
        <met:checkOnly>false</met:checkOnly>
        <met:ignoreWarnings>true</met:ignoreWarnings>
        <met:performRetrieve>false</met:performRetrieve>
        <met:purgeOnDelete>false</met:purgeOnDelete>
        <met:rollbackOnError>true</met:rollbackOnError>
        <met:singlePackage>true</met:singlePackage>
        <met:testLevel>NoTestRun</met:testLevel>
      </met:DeployOptions>
    </met:deploy>
  </soapenv:Body>
</soapenv:Envelope>"""

    resp = requests.post(
        f"https://{instance}/services/Soap/m/{SF_VERSION}",
        data=soap.encode(),
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "deploy"},
        timeout=120,
    )

    # Extract async ID from response
    match = re.search(r"<id>([^<]+)</id>", resp.text)
    if not match:
        snippet = re.sub(r"<[^>]+>", " ", resp.text)[:400]
        raise Exception(f"Deploy call failed: {snippet}")
    return match.group(1)


def sf_check_deploy(session_id: str, instance: str, async_id: str) -> dict:
    soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:SessionHeader><met:sessionId>{session_id}</met:sessionId></met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:checkDeployStatus>
      <met:asyncProcessId>{async_id}</met:asyncProcessId>
      <met:includeDetails>true</met:includeDetails>
    </met:checkDeployStatus>
  </soapenv:Body>
</soapenv:Envelope>"""

    resp = requests.post(
        f"https://{instance}/services/Soap/m/{SF_VERSION}",
        data=soap.encode(),
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "checkDeployStatus"},
        timeout=30,
    )

    root = ET.fromstring(resp.text)
    ns = "http://soap.sforce.com/2006/04/metadata"

    def txt(tag):
        el = root.find(f".//{{{ns}}}{tag}")
        return el.text if el is not None else None

    done = txt("done") == "true"
    success = txt("success") == "true"

    errors = []
    for fail in root.findall(f".//{{{ns}}}componentFailures"):
        prob = fail.find(f"{{{ns}}}problem")
        comp = fail.find(f"{{{ns}}}fullName")
        if prob is not None:
            errors.append(f"{comp.text if comp is not None else '?'}: {prob.text}")

    return {"done": done, "success": success, "errors": errors}


def poll_deploy(session_id: str, instance: str, async_id: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = sf_check_deploy(session_id, instance, async_id)
        if status["done"]:
            return status
        time.sleep(4)
    return {"done": True, "success": False, "errors": ["Deployment timed out"]}


# ── Main streaming endpoint ───────────────────────────────────────────────────

@app.post("/register")
async def register(req: RegisterRequest):
    async def stream() -> AsyncGenerator[str, None]:
        try:
            # Resolve credentials from request or env
            instance_url = req.sf_instance_url or os.getenv("SF_INSTANCE_URL", "")
            username = req.sf_username or os.getenv("SF_USERNAME", "")
            password = req.sf_password or os.getenv("SF_PASSWORD", "")
            token = req.sf_security_token or os.getenv("SF_SECURITY_TOKEN", "")

            if not username or not password:
                yield sse({"done": True, "success": False, "error": "Salesforce credentials not provided."})
                return

            # 1. Login
            yield sse({"step": "Connecting to Salesforce…", "progress": 8})
            session_id, sf_instance = await asyncio.to_thread(sf_login, instance_url, username, password, token)

            # 2. Fetch docs
            yield sse({"step": "Fetching API documentation…", "progress": 20})
            content, content_type = await asyncio.to_thread(fetch_docs, req.url)

            # 3. Derive service name
            if req.service_name:
                raw_name = req.service_name
            else:
                parsed_url = urlparse(req.url)
                host_parts = parsed_url.netloc.split(".")
                raw_name = host_parts[1] if len(host_parts) > 2 else host_parts[0]
                raw_name = raw_name.replace("-", "_").title()

            svc_name = safe_name(raw_name)
            nc_name = f"NC_{svc_name}"

            # 4. Generate spec
            yield sse({"step": "Generating OpenAPI 3.0 specification…", "progress": 35})
            spec, spec_yaml = await asyncio.to_thread(generate_spec, content, content_type, raw_name)

            if not spec.get("paths"):
                yield sse({"done": True, "success": False, "error": "Could not extract any API operations from the docs. Try providing a direct link to the OpenAPI spec."})
                return

            # 5. Extract metadata
            meta = await asyncio.to_thread(spec_metadata, spec)
            yield sse({"step": f"Found {len(meta['operations'])} operations — building metadata…", "progress": 60})

            # 6. Build ZIP
            zip_bytes = await asyncio.to_thread(build_zip, svc_name, nc_name, spec_yaml, meta)

            # 7. Deploy
            yield sse({"step": "Deploying to Salesforce…", "progress": 75})
            async_id = await asyncio.to_thread(sf_deploy, session_id, sf_instance, zip_bytes)

            # 8. Poll
            yield sse({"step": "Waiting for deployment to complete…", "progress": 85})
            status = await asyncio.to_thread(poll_deploy, session_id, sf_instance, async_id)

            if not status["success"]:
                err_detail = "; ".join(status.get("errors", ["Unknown error"]))
                yield sse({"done": True, "success": False, "error": f"Deployment failed: {err_detail}"})
                return

            setup_url = f"https://{sf_instance.replace('.salesforce.com', '.salesforce-setup.com')}/lightning/setup/ExternalServices/home"
            yield sse({
                "done": True,
                "success": True,
                "service_name": svc_name,
                "named_credential": nc_name,
                "host": meta["host"],
                "operations": meta["operations"],
                "setup_url": setup_url,
            })

        except Exception as e:
            yield sse({"done": True, "success": False, "error": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html")) as f:
        return HTMLResponse(f.read())
