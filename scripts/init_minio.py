"""Initialize MinIO bucket on container startup."""

import os
import time
import boto3

def init_minio() -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
        region_name=os.environ.get("S3_REGION", "us-east-1"),
    )
    bucket = os.environ.get("S3_BUCKET", "incubrix")

    for attempt in range(30):
        try:
            s3.create_bucket(Bucket=bucket)
            print(f"Bucket '{bucket}' created successfully")
            return
        except s3.exceptions.BucketAlreadyOwnedByYou:
            print(f"Bucket '{bucket}' already exists")
            return
        except Exception as e:
            print(f"Attempt {attempt + 1}: waiting for MinIO ({e})...")
            time.sleep(1)

    raise RuntimeError(f"Could not connect to MinIO and initialize bucket '{bucket}'")

if __name__ == "__main__":
    init_minio()
