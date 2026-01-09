#!/usr/bin/env python3

import argparse
import json
import sys

import httpx

from app.config import settings

ENDPOINTS = {
    "publish": "/api/pipelines/publish",
    "check-jobs": "/api/pipelines/check-jobs",
    "github-tasks-process": "/api/github-tasks/process",
    "github-tasks-cleanup": "/api/github-tasks/cleanup",
}


def main():
    parser = argparse.ArgumentParser(description="Execute pipeline operations")
    parser.add_argument(
        "endpoint",
        choices=list(ENDPOINTS.keys()),
        help="Which endpoint to call",
    )

    args = parser.parse_args()

    url = f"{settings.base_url}{ENDPOINTS[args.endpoint]}"
    headers = {"Authorization": f"Bearer {settings.admin_token}"}

    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(url, headers=headers)
            response.raise_for_status()

        print(json.dumps(response.json(), indent=2))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 504:
            print(f"HTTP Error 504 (Gateway Timeout): {e.response.text}")
            sys.exit(0)
        print(
            f"HTTP Error {e.response.status_code}: {e.response.text}", file=sys.stderr
        )
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
