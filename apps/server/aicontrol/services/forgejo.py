"""Forgejo REST client.

The API token is held server-side only and is never included in any response. Every
method here returns plain dicts that the API layer reshapes; nothing that touches the
token crosses the wire to the browser.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("aicontrol.forgejo")


class ForgejoError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"forgejo {status}: {message}")
        self.status = status


class ForgejoClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/json"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        response = await self._client.get(path, params={k: v for k, v in params.items()
                                                        if v is not None})
        if response.status_code >= 400:
            raise ForgejoError(response.status_code, response.text[:200])
        return response.json()

    async def fetch_attachment(self, attachment_id: str, *,
                               max_bytes: int) -> tuple[bytes, str]:
        """Download one issue attachment, returning (body, content_type).

        Takes only the attachment id, never a caller-supplied URL: the request target
        is built from our own configured base URL, so this cannot be turned into an
        SSRF primitive.
        """
        response = await self._client.get(
            f"{self.base_url}/attachments/{attachment_id}",
            headers={"Authorization": f"token {self._token}"},
            follow_redirects=False,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            # Forgejo redirects unauthenticated requests to its login page.
            raise ForgejoError(401, "not authorised to read this attachment")
        if response.status_code >= 400:
            raise ForgejoError(response.status_code, response.text[:200])
        body = response.content
        if len(body) > max_bytes:
            raise ForgejoError(413, "attachment is too large to proxy")
        return body, (response.headers.get("content-type") or "").split(";")[0].strip()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = await self._client.post(path, json=payload)
        if response.status_code >= 400:
            raise ForgejoError(response.status_code, response.text[:200])
        return response.json()

    # ---------------------------------------------------------------------- probes

    async def whoami(self) -> dict[str, Any]:
        return await self._get("/user")

    # ----------------------------------------------------------------------- repos

    async def repos(self, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._get("/user/repos", limit=limit)
        return data if isinstance(data, list) else []

    async def repo(self, owner: str, name: str) -> dict[str, Any]:
        return await self._get(f"/repos/{owner}/{name}")

    # ---------------------------------------------------------------------- issues

    async def issues(self, owner: str, name: str, *, state: str = "open",
                     limit: int = 50, page: int = 1) -> list[dict[str, Any]]:
        data = await self._get(f"/repos/{owner}/{name}/issues", state=state,
                               limit=limit, page=page, type="issues")
        return data if isinstance(data, list) else []

    async def issue(self, owner: str, name: str, index: int) -> dict[str, Any]:
        return await self._get(f"/repos/{owner}/{name}/issues/{index}")

    async def issue_comments(self, owner: str, name: str,
                             index: int) -> list[dict[str, Any]]:
        data = await self._get(f"/repos/{owner}/{name}/issues/{index}/comments")
        return data if isinstance(data, list) else []

    async def comment_on_issue(self, owner: str, name: str, index: int,
                               body: str) -> dict[str, Any]:
        return await self._post(f"/repos/{owner}/{name}/issues/{index}/comments",
                                {"body": body})

    # --------------------------------------------------------------- pull requests

    async def pulls(self, owner: str, name: str, *, state: str = "open",
                    limit: int = 50) -> list[dict[str, Any]]:
        data = await self._get(f"/repos/{owner}/{name}/pulls", state=state, limit=limit)
        return data if isinstance(data, list) else []

    async def pull(self, owner: str, name: str, index: int) -> dict[str, Any]:
        return await self._get(f"/repos/{owner}/{name}/pulls/{index}")

    async def pull_files(self, owner: str, name: str, index: int) -> list[dict[str, Any]]:
        data = await self._get(f"/repos/{owner}/{name}/pulls/{index}/files")
        return data if isinstance(data, list) else []

    async def pull_commits(self, owner: str, name: str,
                           index: int) -> list[dict[str, Any]]:
        data = await self._get(f"/repos/{owner}/{name}/pulls/{index}/commits")
        return data if isinstance(data, list) else []


def build_issue_context(issue: dict[str, Any], comments: list[dict[str, Any]], *,
                        repo_slug: str, max_comments: int = 8) -> str:
    """Turn an issue into an agent prompt.

    Deliberately narrow: number, title, body, labels and the human discussion. Forgejo
    metadata an agent cannot act on (avatars, urls, ids, reaction counts) is left out
    so the model's context is not flooded.
    """
    lines = [
        f"You are working on issue #{issue.get('number')} in the {repo_slug} repository.",
        "",
        f"# {issue.get('title', '').strip()}",
    ]
    labels = [l.get("name") for l in (issue.get("labels") or []) if l.get("name")]
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    body = (issue.get("body") or "").strip()
    if body:
        lines += ["", "## Description", body]

    human = [c for c in comments if (c.get("body") or "").strip()][-max_comments:]
    if human:
        lines += ["", "## Discussion"]
        for comment in human:
            author = (comment.get("user") or {}).get("login", "someone")
            text = (comment.get("body") or "").strip()
            lines.append(f"\n**{author}:** {text}")

    acceptance = _acceptance_criteria(body)
    if acceptance:
        lines += ["", "## Acceptance criteria", *acceptance]

    lines += ["", "Implement this issue. Ask before making destructive changes."]
    return "\n".join(lines)


def _acceptance_criteria(body: str) -> list[str]:
    """Lift a checklist or an explicit acceptance-criteria section out of an issue body."""
    if not body:
        return []
    lines = body.splitlines()
    collected: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower().lstrip("#").strip()
        if lowered.startswith(("acceptance criteria", "definition of done")):
            in_section = True
            continue
        if in_section and stripped.startswith("#"):
            break
        if in_section and stripped:
            collected.append(stripped)
        elif stripped.startswith(("- [ ]", "- [x]", "* [ ]", "* [x]")):
            collected.append(stripped)
    # Dedupe while preserving order.
    seen: set[str] = set()
    return [c for c in collected if not (c in seen or seen.add(c))]
