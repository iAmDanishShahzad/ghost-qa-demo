"""FastAPI entry point for the Ghost QA GitHub integration."""

import hashlib
import hmac
import json
import os

from fastapi import FastAPI, HTTPException, Request, status
from github import Github, GithubException
from openai import OpenAI, OpenAIError

app = FastAPI(
    title="Ghost QA",
    description="GitHub webhook service for automated code-quality reviews.",
    version="0.1.0",
)


def fetch_commit_diff(repository_full_name: str, commit_id: str) -> str:
    """Fetch a commit's raw unified diff from GitHub."""
    github_client = Github(os.environ["GITHUB_TOKEN"])
    try:
        repository = github_client.get_repo(repository_full_name)
        commit = repository.get_commit(commit_id)

        # PyGithub exposes commit metadata publicly, while GitHub's diff media
        # type is needed to receive the original unified-diff response.
        _, response = commit._requester.requestJsonAndCheck(
            "GET",
            commit.url,
            headers={"Accept": "application/vnd.github.diff"},
        )
        return response["data"]
    finally:
        github_client.close()


def analyze_diff_for_bugs(diff: str) -> str:
    """Ask OpenAI for a structured QA review of a code diff."""
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model="gpt-5.6",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a Senior QA Engineer analyzing code diffs for logic "
                    "bugs. Return the exact path of the file that needs fixing, "
                    "along with its corrected code snippet."
                ),
            },
            {
                "role": "user",
                "content": f"Analyze this code diff for logic bugs:\n\n{diff}",
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "qa_review",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "fixed_code": {"type": "string"},
                        "explanation": {"type": "string"},
                        "file_path": {"type": "string"},
                    },
                    "required": ["fixed_code", "explanation", "file_path"],
                    "additionalProperties": False,
                },
            },
        },
    )
    return response.choices[0].message.content or "{}"


def create_fix_pull_request(
    repository_full_name: str,
    original_commit_id: str,
    file_path: str,
    fixed_code: str,
    explanation: str,
) -> str:
    """Create a branch, commit the proposed fix, and open a pull request."""
    github_client = Github(os.environ["GITHUB_TOKEN"])
    short_commit_id = original_commit_id[:7]
    branch_name = f"ghost-qa/fix-{short_commit_id}"

    try:
        repository = github_client.get_repo(repository_full_name)
        repository.create_git_ref(
            ref=f"refs/heads/{branch_name}", sha=original_commit_id
        )
        target_file = repository.get_contents(file_path, ref=branch_name)
        repository.update_file(
            path=file_path,
            message=f"Ghost QA fix for {file_path}",
            content=fixed_code,
            sha=target_file.sha,
            branch=branch_name,
        )
        pull_request = repository.create_pull(
            title=f"Ghost QA fix for {file_path}",
            body=explanation,
            head=branch_name,
            base=repository.default_branch,
        )
        return pull_request.html_url
    finally:
        github_client.close()


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Return a lightweight readiness response."""
    return {"status": "ok"}


@app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request) -> dict[str, str]:
    """Verify and accept GitHub webhook deliveries."""
    secret = os.getenv("WEBHOOK_SECRET")
    signature = request.headers.get("X-Hub-Signature-256")

    if not secret or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook signature or secret.",
        )

    payload = await request.body()
    expected_signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    if request.headers.get("X-GitHub-Event") == "push":
        event_payload = await request.json()
        commit_id = event_payload.get("after")
        print(f"Received push for commit: {commit_id}")
        repository_full_name = event_payload.get("repository", {}).get("full_name")

        if commit_id and repository_full_name:
            try:
                diff = fetch_commit_diff(repository_full_name, commit_id)
                print(diff)
            except GithubException as error:
                print(f"Unable to fetch commit diff: {error}")
            else:
                try:
                    review = json.loads(analyze_diff_for_bugs(diff))
                    print(review)
                except (OpenAIError, json.JSONDecodeError) as error:
                    print(f"Unable to analyze commit diff: {error}")
                else:
                    try:
                        pull_request_url = create_fix_pull_request(
                            repository_full_name=repository_full_name,
                            original_commit_id=commit_id,
                            file_path=review["file_path"],
                            fixed_code=review["fixed_code"],
                            explanation=review["explanation"],
                        )
                        print(f"Created pull request: {pull_request_url}")
                    except (GithubException, KeyError) as error:
                        print(f"Unable to create pull request: {error}")

    return {"status": "accepted"}
