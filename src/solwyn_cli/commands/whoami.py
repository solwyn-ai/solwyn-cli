"""Current identity command."""

from typing import Literal

import typer
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from solwyn_cli.auth import (
    AuthError,
    JwtCredential,
    ProjectKeyCredential,
    resolve_credential,
)
from solwyn_cli.client import SolwynClient
from solwyn_cli.commands._shared import exit_with_error
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic
from solwyn_cli.output import OutputError, TableData
from solwyn_cli.runtime import command_context, output_engine


class _Identity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_id: str
    email: str
    display_name: str
    subscription_tier: Literal["free", "founder", "team", "scale"]


class _Project(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str


class _WhoamiData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_type: Literal["jwt", "project_key"]
    auth_source: Literal["keyring", "environment"]
    configured_project: str | None
    user: _Identity | None
    projects: list[_Project]
    hint: str | None


_PROJECTS = TypeAdapter(list[_Project])


def _key_data(credential: ProjectKeyCredential, project: str | None) -> _WhoamiData:
    return _WhoamiData(
        auth_type="project_key",
        auth_source=credential.source,
        configured_project=project,
        user=None,
        projects=[],
        hint=(
            None
            if project is not None
            else "Choose a project with --project, SOLWYN_PROJECT_ID, or config."
        ),
    )


def _jwt_data(context: typer.Context, credential: JwtCredential) -> _WhoamiData:
    runtime = command_context(context)
    try:
        with SolwynClient(
            api_url=runtime.api_url,
            credential=credential,
        ) as client:
            identity_response = client.request("GET", "/api/v1/account/me")
            projects_response = client.request("GET", "/api/v1/projects")
        identity = _Identity.model_validate(identity_response.json())
        projects = _PROJECTS.validate_python(projects_response.json())
    except ApiError as error:
        exit_with_error(context, error)
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    except (ValidationError, ValueError, TypeError):
        exit_with_error(
            context,
            OutputError(
                code="invalid_identity_response",
                message="The identity response was invalid.",
            ),
        )
    return _WhoamiData(
        auth_type="jwt",
        auth_source="keyring",
        configured_project=None,
        user=identity,
        projects=projects,
        hint=None,
    )


def _table(data: _WhoamiData) -> TableData:
    rows: list[tuple[str, str]] = [
        ("Authentication", data.auth_type),
        ("Source", data.auth_source),
    ]
    if data.auth_type == "project_key":
        rows.append(("Configured project", data.configured_project or "not configured"))
        if data.hint is not None:
            rows.append(("Hint", data.hint))
    elif data.user is not None:
        rows.extend(
            [
                ("User", data.user.display_name),
                ("Email", data.user.email),
                ("Tier", data.user.subscription_tier),
            ]
        )
        rows.extend(
            ("Project", f"{project.id} ({project.name})") for project in data.projects
        )
    return TableData(
        columns=("Field", "Value"),
        rows=tuple(rows),
        right_align=("Value",),
    )


def whoami(context: typer.Context) -> None:
    """Show the current Solwyn identity and project context."""
    try:
        credential = resolve_credential()
    except AuthError as error:
        exit_with_error(
            context,
            OutputError(code="authentication_failed", message=str(error)),
            semantic=ErrorSemantic.AUTH,
        )
    if credential is None:
        exit_with_error(
            context,
            OutputError(
                code="authentication_required",
                message="Authentication is required.",
                hint="Set SOLWYN_API_KEY or run solwyn login.",
            ),
            semantic=ErrorSemantic.AUTH,
        )
    runtime = command_context(context)
    data = (
        _key_data(credential, runtime.project)
        if isinstance(credential, ProjectKeyCredential)
        else _jwt_data(context, credential)
    )
    output_engine(context).write_success(
        schema="solwyn.whoami.v1",
        data=data,
        table=_table(data),
    )
