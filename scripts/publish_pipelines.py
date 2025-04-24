#!/usr/bin/env python3

import json

import httpx

from app.config import settings


def main():
    url = f"{settings.base_url}/api/pipelines/publish"
    headers = {"Authorization": f"Bearer {settings.admin_token}"}

    with httpx.Client() as client:
        response = client.post(url, headers=headers)

    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
