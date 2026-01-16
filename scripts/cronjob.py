#!/usr/bin/env python3

import argparse
import json
import sys

import httpx

from app.config import settings

ENDPOINTS = {
    "publish": "/api/pipelines/publish",
    "check-jobs": "/api/pipelines/check-jobs",
    "cleanup-stale": "/api/pipelines/cleanup-stale",
    "github-tasks-process": "/api/github-tasks/process",
    "github-tasks-cleanup": "/api/github-tasks/cleanup",
    "prune-beta": None,
    "prune-stable": None,
}


def main():
    parser = argparse.ArgumentParser(description="Execute pipeline operations")
    parser.add_argument(
        "endpoint",
        choices=list(ENDPOINTS.keys()),
        help="Which endpoint to call",
    )

    args = parser.parse_args()

    if args.endpoint in ("prune-beta", "prune-stable"):
        repo = "beta" if args.endpoint == "prune-beta" else "stable"
        url = f"{settings.flat_manager_url}/api/v1/prune"
        headers = {"Authorization": f"Bearer {settings.flat_manager_token}"}
        data = {"repo": repo}
    else:
        url = f"{settings.base_url}{ENDPOINTS[args.endpoint]}"
        headers = {"Authorization": f"Bearer {settings.admin_token}"}
        data = None

    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(url, headers=headers, json=data)
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
