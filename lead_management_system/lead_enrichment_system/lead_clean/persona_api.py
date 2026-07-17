import os
import json
import uuid
import boto3
from boto3.dynamodb.conditions import Key, Attr
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
from dotenv import load_dotenv

# Load env variables safely using absolute path, or from a cloud-provided path
env_path = os.environ.get("ENV_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
load_dotenv(env_path)

app = FastAPI(title="Persona Builder API", description="Dynamic API for Lead Validation System UI", version="2.0")

# Optional auth: if API_KEY is set in the environment, every request must send
# it in the X-API-Key header. Unset (local development) = open access.
API_KEY = os.environ.get("API_KEY", "").strip()

@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if API_KEY and request.url.path.startswith("/api/") and request.headers.get("x-api-key") != API_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-API-Key header"})
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize DynamoDB Client
dynamodb = boto3.resource(
    'dynamodb',
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
)

LEADS_TABLE_NAME = os.environ.get("AWS_DYNAMODB_TABLE_NAME", "cleaned_data_leads")
PERSONAS_TABLE_NAME = "saved_personas"

leads_table = dynamodb.Table(LEADS_TABLE_NAME)

# Create Personas table if it doesn't exist
def ensure_personas_table():
    try:
        client = boto3.client(
            'dynamodb',
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
        existing_tables = client.list_tables()['TableNames']
        if PERSONAS_TABLE_NAME not in existing_tables:
            print(f"Creating {PERSONAS_TABLE_NAME} table...")
            client.create_table(
                TableName=PERSONAS_TABLE_NAME,
                KeySchema=[{'AttributeName': 'Persona_ID', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'Persona_ID', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST'
            )
            waiter = client.get_waiter('table_exists')
            waiter.wait(TableName=PERSONAS_TABLE_NAME)
    except Exception as e:
        print(f"Error checking/creating table: {e}")

ensure_personas_table()
personas_table = dynamodb.Table(PERSONAS_TABLE_NAME)

# --- Config Loaders ---
CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")

def load_json(filename):
    path = os.path.join(CONFIG_DIR, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

# --- Schemas ---
class SavePersonaRequest(BaseModel):
    name: str
    position: Optional[str] = None
    industry: Optional[str] = None
    domain: Optional[str] = None
    sub_domain: Optional[str] = None
    location: Optional[str] = None

class SavePersonaResponse(BaseModel):
    message: str
    persona_id: str

class PersonaRules(BaseModel):
    position: Optional[str] = None
    industry: Optional[str] = None
    domain: Optional[str] = None
    sub_domain: Optional[str] = None
    location: Optional[str] = None

class Persona(BaseModel):
    Persona_ID: str
    Name: str
    rules: PersonaRules

# --- 1. Dropdown Populator APIs ---

@app.get("/api/options/positions", response_model=List[str])
def get_positions():
    data = load_json("positions.json")
    return list(data.keys()) if isinstance(data, dict) else data

@app.get("/api/options/industries", response_model=List[str])
def get_industries():
    data = load_json("industries.json")
    return list(data.keys()) if isinstance(data, dict) else data

@app.get("/api/options/domains", response_model=List[str])
def get_domains():
    data = load_json("domains_subdomains.json")
    taxonomy = data.get("taxonomy", [])
    domains = [item.get("domain") for item in taxonomy if "domain" in item]
    return sorted(domains)

@app.get("/api/options/sub_domains", response_model=List[str])
def get_sub_domains(domain: Optional[str] = None):
    data = load_json("domains_subdomains.json")
    taxonomy = data.get("taxonomy", [])
    
    if domain:
        for item in taxonomy:
            if item.get("domain") == domain:
                return item.get("subdomains", [])
        return []
    
    all_sub_domains = set()
    for item in taxonomy:
        for sub in item.get("subdomains", []):
            all_sub_domains.add(sub)
            
    return sorted(list(all_sub_domains))

@app.get("/api/options/locations", response_model=List[str])
def get_locations():
    data = load_json("locations.json")
    return data if isinstance(data, list) else []

@app.get("/api/options/keywords", response_model=List[str])
def get_keywords():
    data = load_json("unique_keywords.json")
    # Keywords file is extremely large (71k+ items), so we slice it to prevent overwhelming the UI
    # In a production app with this many keywords, you would use a search/autocomplete endpoint
    return data[:1000] if isinstance(data, list) else []

# --- 2. Unified Dynamic Search API ---

# The strict ordering of columns required for consistent JSON responses
LEAD_COLUMNS = [
    "User_ID", "First Name", "Last Name", "Title", "Position", "Company Name", 
    "Company Email", "Contact Number_1", "Contact Number_2", "No of Employees", 
    "Industry", "Keywords", "Person LinkedIn", "Company LinkedIn", "Company Website", 
    "Company Facebook", "Company Twitter", "City", "State", "Country", 
    "Annual Revenue", "Last Revenue", "Total Funding", "Last Funding", 
    "Domain", "Sub Domain", "Company Scale"
]

def format_lead(item: dict) -> dict:
    """Forces the DynamoDB unordered dict into a strictly ordered dict."""
    return {col: item.get(col, "") for col in LEAD_COLUMNS}

# DynamoDB applies FilterExpression AFTER reading a page, so a single scan/query
# call with Limit only inspects that many rows and can miss almost every match.
# These helpers keep paging until enough matches are collected or the data ends.
MAX_PAGES = 200  # hard safety cap (~200MB examined)

def paginated_scan(table, filter_expression=None, match_limit=200):
    items, kwargs, pages = [], {}, 0
    if filter_expression is not None:
        kwargs['FilterExpression'] = filter_expression
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
        pages += 1
        last_key = response.get('LastEvaluatedKey')
        if len(items) >= match_limit or not last_key or pages >= MAX_PAGES:
            return items[:match_limit]
        kwargs['ExclusiveStartKey'] = last_key

def paginated_query(table, query_kwargs, match_limit=200):
    items, pages = [], 0
    kwargs = dict(query_kwargs)
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        pages += 1
        last_key = response.get('LastEvaluatedKey')
        if len(items) >= match_limit or not last_key or pages >= MAX_PAGES:
            return items[:match_limit]
        kwargs['ExclusiveStartKey'] = last_key

@app.get("/api/leads/search", response_model=List[Dict])
def search_leads(
    position: Optional[str] = None,
    industry: Optional[str] = None,
    domain: Optional[str] = None,
    sub_domain: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = 200
):
    limit = max(1, min(limit, 1000))
    target_table = leads_table
    
    query_kwargs = {}
    filters = []
    
    param_map = {
        'position': ('Position', 'Position-Index'),
        'industry': ('Industry', 'Industry-Index'),
        'domain': ('Domain', 'Domain-Index'),
        'sub_domain': ('Sub Domain', 'SubDomain-Index')
    }
    
    active_params = {}
    if position: active_params['position'] = position
    if industry: active_params['industry'] = industry
    if domain: active_params['domain'] = domain
    if sub_domain: active_params['sub_domain'] = sub_domain

    # Process location filter separately since it uses OR conditions across 3 columns
    location_filter = None
    if location:
        # Split comma separated locations and build the OR condition
        loc_list = [l.strip() for l in location.split(',')]
        location_filter = Attr('City').is_in(loc_list) | Attr('State').is_in(loc_list) | Attr('Country').is_in(loc_list)

    if not active_params and not location_filter:
        return [format_lead(i) for i in paginated_scan(target_table, None, limit)]

    if not active_params:
        # If ONLY location is provided, we must use a Scan since there is no index for location
        try:
            return [format_lead(i) for i in paginated_scan(target_table, location_filter, limit)]
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    primary_key = list(active_params.keys())[0]
    primary_attr, index_name = param_map[primary_key]
    primary_value = active_params.pop(primary_key)
    
    query_kwargs['IndexName'] = index_name
    query_kwargs['KeyConditionExpression'] = Key(primary_attr).eq(primary_value)
    
    for key, value in active_params.items():
        attr_name, _ = param_map[key]
        filters.append(Attr(attr_name).eq(value))
        
    if location_filter:
        filters.append(location_filter)
        
    if filters:
        combined_filter = filters[0]
        for f in filters[1:]:
            combined_filter = combined_filter & f
        query_kwargs['FilterExpression'] = combined_filter
        
    try:
        return [format_lead(i) for i in paginated_query(target_table, query_kwargs, limit)]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/leads/search/raw", response_model=List[Dict])
def search_raw_leads(
    title: Optional[str] = None,
    industry: Optional[str] = None,
    keywords: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = 200
):
    limit = max(1, min(limit, 1000))
    raw_table = dynamodb.Table(os.environ.get("AWS_DYNAMODB_RAW_TABLE_NAME", "raw_data_leads"))
    filters = []
    
    if title:
        f = Attr('Title').contains(title) | Attr('Title').contains(title.lower()) | Attr('Title').contains(title.title()) | Attr('Title').contains(title.upper())
        filters.append(f)
        
    if industry:
        f = Attr('Industry').contains(industry) | Attr('Industry').contains(industry.lower()) | Attr('Industry').contains(industry.title()) | Attr('Industry').contains(industry.upper())
        filters.append(f)
        
    if keywords:
        f = Attr('Keywords').contains(keywords) | Attr('Keywords').contains(keywords.lower()) | Attr('Keywords').contains(keywords.title()) | Attr('Keywords').contains(keywords.upper())
        filters.append(f)
        
    if location:
        loc_list = [l.strip() for l in location.split(',')]
        loc_filters = []
        for loc in loc_list:
            loc_f = (
                Attr('City').contains(loc) | Attr('City').contains(loc.lower()) | Attr('City').contains(loc.title()) | Attr('City').contains(loc.upper()) |
                Attr('State').contains(loc) | Attr('State').contains(loc.lower()) | Attr('State').contains(loc.title()) | Attr('State').contains(loc.upper()) |
                Attr('Country').contains(loc) | Attr('Country').contains(loc.lower()) | Attr('Country').contains(loc.title()) | Attr('Country').contains(loc.upper())
            )
            loc_filters.append(loc_f)
            
        combined_loc = loc_filters[0]
        for lf in loc_filters[1:]:
            combined_loc = combined_loc | lf
        filters.append(combined_loc)
        
    if not filters:
        return [format_lead(i) for i in paginated_scan(raw_table, None, limit)]

    combined_filter = filters[0]
    for f in filters[1:]:
        combined_filter = combined_filter & f

    try:
        return [format_lead(i) for i in paginated_scan(raw_table, combined_filter, limit)]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- 3. Person Lookup APIs (find specific people by name / company / linkedin) ---

class PersonQuery(BaseModel):
    name: Optional[str] = None                # first, last, or "first last" (either order)
    organization_name: Optional[str] = None   # matches Company Name (contains)
    linkedin_id: Optional[str] = None         # matches Person LinkedIn (slug or full URL)
    position: Optional[str] = None
    industry: Optional[str] = None
    domain: Optional[str] = None
    sub_domain: Optional[str] = None
    location: Optional[str] = None

class PersonSearchResult(BaseModel):
    query: PersonQuery
    count: int
    leads: List[Dict]

def build_name_filter(name_term: str):
    """
    Fully case-insensitive partial name matching via the normalized Name_Search
    attribute ("first last", lowercase). Every word the user types must appear
    somewhere in the full name, so "joe", "deruvo", "joe deruvo" and
    "deruvo joe" all find the same person - and camelCase/McNames can't hide.
    """
    combined = None
    for token in name_term.lower().split():
        cond = Attr('Name_Search').contains(token)
        combined = cond if combined is None else (combined & cond)
    return combined

def build_person_extra_filter(q: PersonQuery):
    """AND-combined filter for every non-name field present in one person query."""
    filters = []
    if q.organization_name:
        filters.append(Attr('Company_Search').contains(q.organization_name.strip().lower()))
    if q.linkedin_id:
        li = q.linkedin_id.strip()
        filters.append(Attr('Person LinkedIn').contains(li) | Attr('Person LinkedIn').contains(li.lower()))
    if q.position: filters.append(Attr('Position').eq(q.position))
    if q.industry: filters.append(Attr('Industry').eq(q.industry))
    if q.domain: filters.append(Attr('Domain').eq(q.domain))
    if q.sub_domain: filters.append(Attr('Sub Domain').eq(q.sub_domain))
    if q.location:
        loc_list = [l.strip() for l in q.location.split(',')]
        filters.append(Attr('City').is_in(loc_list) | Attr('State').is_in(loc_list) | Attr('Country').is_in(loc_list))
    combined = None
    for f in filters:
        combined = f if combined is None else (combined & f)
    return combined

def find_person_leads(name: Optional[str], extra_filter, limit: int):
    """Indexed fast path for exact full names via NameSearch-Index; scan fallback for partials."""
    if name and " " in name.strip():
        normalized = " ".join(name.split()).lower()
        query_kwargs = {'IndexName': 'NameSearch-Index',
                        'KeyConditionExpression': Key('Name_Search').eq(normalized)}
        if extra_filter is not None:
            query_kwargs['FilterExpression'] = extra_filter
        try:
            items = paginated_query(leads_table, query_kwargs, limit)
            if items:
                return items
        except Exception:
            pass  # index unavailable -> fall through to scan
    combined = build_name_filter(name) if name else None
    if extra_filter is not None:
        combined = extra_filter if combined is None else (combined & extra_filter)
    if combined is None:
        return []
    return paginated_scan(leads_table, combined, limit)

@app.get("/api/leads/person", response_model=List[Dict])
def search_person(
    name: Optional[str] = None,            # comma-separated: "priya, sharma, rob hirschfeld"
    organization: Optional[str] = None,
    linkedin: Optional[str] = None,
    limit: int = 200
):
    limit = max(1, min(limit, 1000))
    if not (name or organization or linkedin):
        raise HTTPException(status_code=400, detail="Provide at least one of: name, organization, linkedin")
    extra = build_person_extra_filter(PersonQuery(organization_name=organization, linkedin_id=linkedin))
    names = [n.strip() for n in name.split(',') if n.strip()] if name else [None]
    seen, results = set(), []
    for n in names:
        for item in find_person_leads(n, extra, limit):
            uid = item.get('User_ID')
            if uid not in seen:
                seen.add(uid)
                results.append(format_lead(item))
            if len(results) >= limit:
                return results
    return results

@app.post("/api/leads/people", response_model=List[PersonSearchResult])
def search_people(queries: List[PersonQuery], limit_per_person: int = 50):
    if not queries:
        raise HTTPException(status_code=400, detail="Send a list with at least one person query")
    if len(queries) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 person queries per request")
    limit_per_person = max(1, min(limit_per_person, 500))
    results = []
    for q in queries:
        if not (q.name or q.organization_name or q.linkedin_id):
            results.append(PersonSearchResult(query=q, count=0, leads=[]))
            continue
        extra = build_person_extra_filter(q)
        leads = [format_lead(i) for i in find_person_leads(q.name, extra, limit_per_person)]
        results.append(PersonSearchResult(query=q, count=len(leads), leads=leads))
    return results

# --- 4. Persona Management APIs ---

@app.post("/api/personas", response_model=SavePersonaResponse)
def save_persona(request: SavePersonaRequest):
    persona_id = f"PRS-{str(uuid.uuid4())[:8].upper()}"
    
    item = {
        'Persona_ID': persona_id,
        'Name': request.name,
        'rules': {
            'position': request.position,
            'industry': request.industry,
            'domain': request.domain,
            'sub_domain': request.sub_domain,
            'location': request.location
        }
    }
    
    try:
        personas_table.put_item(Item=item)
        return {"message": "Persona saved successfully", "persona_id": persona_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/personas", response_model=List[Persona])
def list_personas(search: Optional[str] = None):
    try:
        response = personas_table.scan()
        items = response.get('Items', [])
        
        if search:
            search_lower = search.lower()
            items = [
                i for i in items 
                if search_lower in i.get('Name', '').lower() 
                or search_lower in i.get('Persona_ID', '').lower()
            ]
            
        return items
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/personas/{persona_id}/leads", response_model=List[Dict])
def get_leads_for_persona(persona_id: str):
    try:
        response = personas_table.get_item(Key={'Persona_ID': persona_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if 'Item' not in response:
        raise HTTPException(status_code=404, detail="Persona not found")
    persona_data = response['Item']
        
    rules = persona_data.get('rules', {})
    
    return search_leads(
        position=rules.get('position'),
        industry=rules.get('industry'),
        domain=rules.get('domain'),
        sub_domain=rules.get('sub_domain'),
        location=rules.get('location')
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("persona_api:app", host="127.0.0.1", port=8000, reload=True)
