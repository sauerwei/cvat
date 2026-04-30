#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, build_opener


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "backups"
DEFAULT_PAGE_SIZE = 100


class ApiError(RuntimeError):
    pass


@dataclass
class ApiClient:
    base_url: str
    auth_header: str

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/") + "/"
        self._opener = build_opener()

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        expected_statuses: tuple[int, ...],
    ) -> tuple[int, bytes]:
        url = path_or_url if path_or_url.startswith("http") else urljoin(self.base_url, path_or_url)
        request = Request(
            url,
            headers={
                "Authorization": self.auth_header,
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with self._opener.open(request) as response:
                body = response.read()
                status = response.status
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise ApiError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ApiError(f"{method} {url} failed: {exc.reason}") from exc

        if status not in expected_statuses:
            detail = body.decode("utf-8", errors="replace").strip()
            raise ApiError(f"{method} {url} returned HTTP {status}: {detail}")

        return status, body

    def get_json(self, path_or_url: str) -> Any:
        _, body = self.request("GET", path_or_url, expected_statuses=(200,))
        return json.loads(body.decode("utf-8"))

    def post_json(self, path_or_url: str) -> Any:
        _, body = self.request("POST", path_or_url, expected_statuses=(201, 202, 409))
        return json.loads(body.decode("utf-8"))

    def download(self, path_or_url: str, destination: Path) -> None:
        url = path_or_url if path_or_url.startswith("http") else urljoin(self.base_url, path_or_url)
        request = Request(url, headers={"Authorization": self.auth_header}, method="GET")
        tmp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            with self._opener.open(request) as response, tmp_path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise ApiError(f"GET {url} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ApiError(f"GET {url} failed: {exc.reason}") from exc
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        else:
            tmp_path.replace(destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download ZIP backups for all accessible CVAT projects through the REST API."
    )
    parser.add_argument("--base-url", default=os.environ.get("CVAT_BASE_URL", ""))
    parser.add_argument("--access-token", default=os.environ.get("CVAT_ACCESS_TOKEN", ""))
    parser.add_argument("--username", default=os.environ.get("CVAT_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("CVAT_PASSWORD", ""))
    parser.add_argument(
        "--output-root",
        default=os.environ.get("CVAT_API_BACKUP_ROOT", str(DEFAULT_OUTPUT_ROOT)),
    )
    parser.add_argument(
        "--page-size",
        default=int(os.environ.get("CVAT_API_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
        type=int,
    )
    parser.add_argument(
        "--poll-interval",
        default=float(os.environ.get("CVAT_API_POLL_INTERVAL", "2")),
        type=float,
    )
    parser.add_argument(
        "--request-timeout",
        default=int(os.environ.get("CVAT_API_REQUEST_TIMEOUT", "1800")),
        type=int,
    )
    parser.add_argument(
        "--lightweight",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("CVAT_API_BACKUP_LIGHTWEIGHT", "true").lower() not in {"0", "false", "no"},
    )
    args = parser.parse_args()

    if not args.base_url:
        parser.error("Set --base-url or CVAT_BASE_URL.")
    if not args.access_token and not (args.username and args.password):
        parser.error("Provide an access token or a username/password pair.")

    return args


def build_auth_header(args: argparse.Namespace) -> str:
    if args.access_token:
        return f"Bearer {args.access_token}"
    credentials = f"{args.username}:{args.password}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


def sanitize_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    sanitized = sanitized.strip("._-")
    return sanitized or "project"


def list_projects(client: ApiClient, page_size: int) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    next_url = f"api/projects?page_size={page_size}"
    while next_url:
        payload = client.get_json(next_url)
        projects.extend(payload.get("results", []))
        next_url = payload.get("next")
    return projects


def initiate_backup(client: ApiClient, project: dict[str, Any], *, lightweight: bool) -> str:
    project_id = project["id"]
    project_name = sanitize_name(project.get("name") or f"project_{project_id}")
    filename = quote(f"project_{project_id}_{project_name}_backup.zip")
    payload = client.post_json(
        f"api/projects/{project_id}/backup/export"
        f"?filename={filename}&lightweight={'true' if lightweight else 'false'}"
    )
    rq_id = payload.get("rq_id")
    if not rq_id:
        raise ApiError(f"Backup request for project {project_id} returned no rq_id.")
    return rq_id


def wait_for_request(
    client: ApiClient,
    rq_id: str,
    *,
    poll_interval: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        payload = client.get_json(f"api/requests/{rq_id}")
        status = payload.get("status")
        if status == "finished":
            return payload
        if status == "failed":
            message = payload.get("message") or "No error details returned by CVAT."
            raise ApiError(f"Request {rq_id} failed: {message}")
        if time.monotonic() >= deadline:
            raise ApiError(f"Request {rq_id} did not finish within {timeout_seconds} seconds.")
        time.sleep(poll_interval)


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    client = ApiClient(base_url=args.base_url, auth_header=build_auth_header(args))

    run_dir = (
        Path(args.output_root).expanduser().resolve()
        / f"projects_api_{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d_%H%M%S')}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "base_url": args.base_url.rstrip("/"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "lightweight": args.lightweight,
        "projects": [],
        "failures": [],
    }

    try:
        projects = list_projects(client, args.page_size)
        print(f"Found {len(projects)} accessible project(s).")

        for index, project in enumerate(projects, start=1):
            project_id = project["id"]
            project_name = project.get("name") or f"project_{project_id}"
            filename = f"{project_id:06d}_{sanitize_name(project_name)}.zip"
            output_path = run_dir / filename
            print(f"[{index}/{len(projects)}] Backing up project {project_id} ({project_name})")

            project_manifest = {
                "id": project_id,
                "name": project_name,
                "output_file": filename,
            }
            try:
                rq_id = initiate_backup(client, project, lightweight=args.lightweight)
                project_manifest["rq_id"] = rq_id
                request_info = wait_for_request(
                    client,
                    rq_id,
                    poll_interval=args.poll_interval,
                    timeout_seconds=args.request_timeout,
                )
                result_url = request_info.get("result_url")
                if not result_url:
                    raise ApiError(f"Request {rq_id} finished without a result_url.")
                client.download(result_url, output_path)
                project_manifest["size_bytes"] = output_path.stat().st_size
                manifest["projects"].append(project_manifest)
            except Exception as exc:
                project_manifest["error"] = str(exc)
                manifest["failures"].append(project_manifest)
                print(f"  FAILED: {exc}", file=sys.stderr)

        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["successful_project_count"] = len(manifest["projects"])
        manifest["failed_project_count"] = len(manifest["failures"])
        write_manifest(run_dir / "manifest.json", manifest)
    except Exception as exc:
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["fatal_error"] = str(exc)
        write_manifest(run_dir / "manifest.json", manifest)
        print(f"Backup run failed: {exc}", file=sys.stderr)
        return 1

    if manifest["failures"]:
        print(
            f"Backup run completed with failures. Successful: {manifest['successful_project_count']}, "
            f"failed: {manifest['failed_project_count']}. See {run_dir / 'manifest.json'}",
            file=sys.stderr,
        )
        return 2

    print(
        f"Backup run completed successfully. Exported {manifest['successful_project_count']} project(s) to {run_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
