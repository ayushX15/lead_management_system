"""
One-time AWS provisioning for the Lead Clean Pipeline.

Creates (idempotently - existing resources are left untouched):
  - S3 bucket           <AWS_S3_RAW_BUCKET_NAME>   (raw CSV drop + state/ backups)
  - cleaned_data_leads  (PK User_ID) + 4 GSIs used by the search API
  - raw_data_leads      (PK User_ID)               untouched raw replica
  - duplicate_raw_leads (PK User_ID, SK Duplicate_ID) duplicate copies grouped by lead
  - cleaned_leads_scrape_context (PK User_ID)      website scrape text per lead
  - saved_personas      (PK Persona_ID)            saved UI filter profiles

Run from the repo root:  python lead_clean/scripts/aws_setup.py
"""

import os
import boto3
from dotenv import load_dotenv

load_dotenv(".env")

AWS_KWARGS = dict(
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
)

s3_client = boto3.client('s3', **AWS_KWARGS)
dynamodb = boto3.client('dynamodb', **AWS_KWARGS)

SEARCH_GSIS = [
    ("Industry-Index", "Industry"),
    ("Domain-Index", "Domain"),
    ("SubDomain-Index", "Sub Domain"),
    ("Position-Index", "Position"),
    ("NameSearch-Index", "Name_Search"),
]


def create_s3_bucket(bucket_name):
    try:
        region = AWS_KWARGS["region_name"]
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(Bucket=bucket_name,
                                    CreateBucketConfiguration={'LocationConstraint': region})
        print(f"[OK] S3 bucket '{bucket_name}' created.")
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        print(f"[OK] S3 bucket '{bucket_name}' already exists (yours).")
    except s3_client.exceptions.BucketAlreadyExists:
        print(f"[!!] Bucket name '{bucket_name}' is taken globally. Pick another name in .env.")
    except Exception as e:
        print(f"[!!] Error creating bucket '{bucket_name}': {e}")


def create_table(table_name, key_schema, attribute_definitions, gsis=None):
    try:
        kwargs = dict(
            TableName=table_name,
            KeySchema=key_schema,
            AttributeDefinitions=attribute_definitions,
            BillingMode='PAY_PER_REQUEST',
        )
        if gsis:
            kwargs['GlobalSecondaryIndexes'] = [
                {'IndexName': name,
                 'KeySchema': [{'AttributeName': attr, 'KeyType': 'HASH'}],
                 'Projection': {'ProjectionType': 'ALL'}}
                for name, attr in gsis
            ]
        dynamodb.create_table(**kwargs)
        dynamodb.get_waiter('table_exists').wait(TableName=table_name)
        print(f"[OK] Table '{table_name}' created.")
    except dynamodb.exceptions.ResourceInUseException:
        print(f"[OK] Table '{table_name}' already exists.")
    except Exception as e:
        print(f"[!!] Error creating table '{table_name}': {e}")


def main():
    bucket = os.environ.get("AWS_S3_RAW_BUCKET_NAME", "raw-data-leads")
    cleaned = os.environ.get("AWS_DYNAMODB_TABLE_NAME", "cleaned_data_leads")
    raw = os.environ.get("AWS_DYNAMODB_RAW_TABLE_NAME", "raw_data_leads")

    create_s3_bucket(bucket)

    create_table(cleaned,
                 [{'AttributeName': 'User_ID', 'KeyType': 'HASH'}],
                 [{'AttributeName': 'User_ID', 'AttributeType': 'S'},
                  {'AttributeName': 'Industry', 'AttributeType': 'S'},
                  {'AttributeName': 'Domain', 'AttributeType': 'S'},
                  {'AttributeName': 'Sub Domain', 'AttributeType': 'S'},
                  {'AttributeName': 'Position', 'AttributeType': 'S'},
                  {'AttributeName': 'Name_Search', 'AttributeType': 'S'}],
                 gsis=SEARCH_GSIS)

    create_table(raw,
                 [{'AttributeName': 'User_ID', 'KeyType': 'HASH'}],
                 [{'AttributeName': 'User_ID', 'AttributeType': 'S'}])

    create_table("duplicate_raw_leads",
                 [{'AttributeName': 'User_ID', 'KeyType': 'HASH'},
                  {'AttributeName': 'Duplicate_ID', 'KeyType': 'RANGE'}],
                 [{'AttributeName': 'User_ID', 'AttributeType': 'S'},
                  {'AttributeName': 'Duplicate_ID', 'AttributeType': 'S'}])

    create_table("cleaned_leads_scrape_context",
                 [{'AttributeName': 'User_ID', 'KeyType': 'HASH'}],
                 [{'AttributeName': 'User_ID', 'AttributeType': 'S'}])

    create_table("saved_personas",
                 [{'AttributeName': 'Persona_ID', 'KeyType': 'HASH'}],
                 [{'AttributeName': 'Persona_ID', 'AttributeType': 'S'}])

    print("\nAWS infrastructure is ready. Drop your raw CSVs under s3://" + bucket + "/raw/")


if __name__ == "__main__":
    main()
