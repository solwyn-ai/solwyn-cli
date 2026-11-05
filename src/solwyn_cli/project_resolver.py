"""One project resolver: which project a command means, and with which key.

Every command that names a project goes through here (``runtime.resolve_project``
is the entry point). The selector is the first of ``--project``,
``SOLWYN_PROJECT`` and the repo's ``.solwyn.env``; a ``proj_`` id is used as
is, and a name becomes an id by the method the command's credential policy
fixes before any resolution:

========================  ===================================================
purpose                   key for spend, and how a name becomes an id
========================  ===================================================
``read``                  today's credential order. A login resolves names
                          through ``GET /api/v1/projects``; an API key through
                          ``GET /api/v1/auth/key`` with that key; with neither,
                          a stored key is chosen by the local stored names.
``spend``                 ``SOLWYN_API_KEY``, else the stored key (MCP and the
                          metering commands). An env key resolves through
                          ``/auth/key``; a stored key as ``keys_env``.
``keys_env``              the stored key only, never ``SOLWYN_API_KEY``. With
                          a login, ``/api/v1/projects``; without, the local
                          stored names. Never ``/auth/key``.
``with_key``              the key ``login --with-key`` was given:
                          ``/auth/key`` with ``Solwyn-Project: <value>``.
``setup``                 the login: ``/api/v1/projects``.
========================  ===================================================

A present login (``SOLWYN_TOKEN`` or the stored one) decides how a stored
key's name becomes an id, whichever credential sends the request, even under
``SOLWYN_AUTH=project_key``: through that login's projects, never by matching
stored names across accounts. Local name matching applies only with no login.

Whenever a login is present and a name is resolved or a key is created or
stored, the login's account must be known first: a stored login records it;
``SOLWYN_TOKEN`` is asked through ``GET /api/v1/accounts/me``, and if that
fails the command stops (``account_unresolved``), with no fallback to stored
names. Stored keys are only ever selected by an exact id.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError

from solwyn_cli import auth
from solwyn_cli._vendor.validation import (
    PROJECT_ID_PATTERN,
    PROJECT_SELECTOR_MAX_LENGTH,
    canonical_project_id,
    is_project_id,
    trim_selector,
)
from solwyn_cli.auth import ApiKeyCredential, AuthError, LoginCredential
from solwyn_cli.errors import ApiError
from solwyn_cli.exit_codes import ErrorSemantic

Purpose: TypeAlias = Literal["read", "spend", "keys_env", "with_key", "setup"]
ProjectSource: TypeAlias = Literal[
    "cli", "environment", "repo", "legacy_environment", "config", "none"
]

REPO_FILE_NAME = ".solwyn.env"
REPO_VARIABLE = "SOLWYN_PROJECT"
SOURCE_LABELS: dict[str, str] = {
    "cli": "--project",
    "environment": "SOLWYN_PROJECT",
    "repo": REPO_FILE_NAME,
    "legacy_environment": "SOLWYN_PROJECT_ID",
    "config": "config default_project",
    "none": "nothing",
}


class ProjectResolutionError(Exception):
    """A project could not be resolved; the message is safe to show."""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        *,
        semantic: ErrorSemantic = ErrorSemantic.USAGE,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.semantic = semantic


def account_unresolved() -> ProjectResolutionError:
    return ProjectResolutionError(
        "account_unresolved",
        "Could not confirm which account your Solwyn login belongs to. Check your "
        "connection and try again.",
        semantic=ErrorSemantic.AUTH,
    )


def project_required() -> ProjectResolutionError:
    return ProjectResolutionError(
        "project_required",
        "A project is required.",
        "Choose a project with --project or SOLWYN_PROJECT (a project name or "
        "proj_ id), or set SOLWYN_PROJECT in the repo's .solwyn.env.",
    )


def api_key_not_stored(project: str) -> ProjectResolutionError:
    return ProjectResolutionError(
        "api_key_not_stored",
        f"No API key for {project} is stored on this computer.",
        f"Run solwyn keys create --project {project} --store-key.",
        semantic=ErrorSemantic.AUTH,
    )


# --- The repo file ------------------------------------------------------------


class RepoFileError(ValueError):
    """``.solwyn.env`` exists but is not the one assignment it may hold."""


def git_root(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` that holds ``.git``."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def find_repo_file(start: Path | None = None) -> Path | None:
    """``.solwyn.env`` from ``start`` (cwd) up to the git root, if any."""
    try:
        here = (start or Path.cwd()).resolve()
    except OSError:
        return None
    root = git_root(here)
    if root is None:
        return None
    for directory in (here, *here.parents):
        candidate = directory / REPO_FILE_NAME
        if candidate.is_file():
            return candidate
        if directory == root:
            break
    return None


def parse_repo_file(text: str) -> str:
    """The ``SOLWYN_PROJECT`` value of a repo file, or RepoFileError.

    Accepts that one assignment (optional surrounding whitespace, optional
    single or double quotes), comments and blank lines; nothing else.
    """
    value: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw = stripped.partition("=")
        if not separator or name.strip() != REPO_VARIABLE or value is not None:
            raise RepoFileError("unexpected line")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        elif raw[:1] in "\"'" or raw[-1:] in "\"'":
            raise RepoFileError("unbalanced quotes")
        raw = trim_selector(raw)
        if not raw or any(character in raw for character in "\"'$`\\"):
            raise RepoFileError("invalid value")
        value = raw
    if value is None:
        raise RepoFileError("no assignment")
    return value


def read_repo_project(start: Path | None = None) -> tuple[str | None, str | None]:
    """(value, None), (None, error message) or (None, None) without a file."""
    path = find_repo_file(start)
    if path is None:
        return None, None
    try:
        return parse_repo_file(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, RepoFileError):
        return None, (
            f'{path} must hold one line, SOLWYN_PROJECT="<project name or '
            'proj_ id>", plus comments.'
        )


# --- Server reads -----------------------------------------------------------


class _AccountRef(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str
    display_name: str


class _ProjectRef(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    id: str
    name: str


_PROJECT_REFS = TypeAdapter(list[_ProjectRef])


class AuthKeyResponse(BaseModel):
    """``GET /api/v1/auth/key``: the key and the project Solwyn-Project named."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    account_id: str
    key_id: str
    key_prefix: str
    key_name: str
    scope: Literal["full", "read_only"]
    created_via: Literal["dashboard", "cli"]
    expires_at: AwareDatetime
    rotation_grace_until: AwareDatetime | None
    project_id: str
    project_name: str
    budget_mode: Literal["alert_only", "hard_deny"]


ClientFactory: TypeAlias = Callable[..., Any]


def _client_factory() -> ClientFactory:
    from solwyn_cli import client

    return client.SolwynClient


@dataclass
class _Reads:
    """The server reads one resolution makes, each at most once."""

    api_url: str
    login: LoginCredential | None
    factory: ClientFactory | None = None
    _account: tuple[str, str] | None = None
    _projects: list[_ProjectRef] | None = None
    _auth_key: dict[str, AuthKeyResponse] = field(default_factory=dict)

    def _client(self, **kwargs: Any) -> Any:
        # Looked up at call time, so tests can route reads to a fake Core.
        factory = self.factory or _client_factory()
        return factory(**kwargs)

    def account(self) -> tuple[str, str]:
        """The login's (account id, account name); fail closed."""
        if self._account is not None:
            return self._account
        login = self.login
        if login is None:
            raise account_unresolved()
        if login.meta is not None:
            self._account = (login.meta.account_id, login.meta.account_name)
            return self._account
        try:
            with self._client(api_url=self.api_url, credential=login) as client:
                response = client.request("GET", "/api/v1/accounts/me")
            account = _AccountRef.model_validate(response.json())
        except (ApiError, AuthError, ValidationError, ValueError, TypeError):
            raise account_unresolved() from None
        self._account = (account.id, account.display_name)
        return self._account

    def projects(self) -> list[_ProjectRef]:
        if self._projects is None:
            with self._client(api_url=self.api_url, credential=self.login) as client:
                response = client.request("GET", "/api/v1/projects")
            try:
                self._projects = _PROJECT_REFS.validate_python(response.json())
            except (ValidationError, ValueError, TypeError):
                raise ProjectResolutionError(
                    "invalid_projects_response",
                    "The projects response was invalid.",
                    semantic=ErrorSemantic.GENERIC,
                ) from None
        return self._projects

    def auth_key(self, key: ApiKeyCredential, selector: str) -> AuthKeyResponse:
        cached = self._auth_key.get(selector)
        if cached is not None:
            return cached
        probe = key.model_copy(update={"project": selector})
        with self._client(api_url=self.api_url, credential=probe) as client:
            response = client.request("GET", "/api/v1/auth/key")
        try:
            info = AuthKeyResponse.model_validate(response.json())
        except (ValidationError, ValueError, TypeError):
            raise ProjectResolutionError(
                "invalid_auth_key_response",
                "The API key response was invalid.",
                semantic=ErrorSemantic.GENERIC,
            ) from None
        if PROJECT_ID_PATTERN.fullmatch(info.project_id) is None:
            raise ProjectResolutionError(
                "invalid_auth_key_response",
                "The API key response was invalid.",
                semantic=ErrorSemantic.GENERIC,
            )
        self._auth_key[selector] = info
        return info


# --- Resolution -------------------------------------------------------------


@dataclass(frozen=True)
class Candidates:
    """The selector and, for the mismatch check, the repo file's value."""

    selected: str | None
    source: ProjectSource
    repo: str | None
    repo_error: str | None = None


@dataclass(frozen=True)
class ProjectSelection:
    """What the resolver decided. ``credential`` is the key bound for spend."""

    selector: str | None
    source: ProjectSource
    project_id: str | None
    project_name: str | None = None
    account_id: str | None = None
    credential: auth.ResolvedCredential | None = None
    warning: str | None = None


def _mismatch_message(candidates: Candidates) -> str:
    return (
        f"This repo is set to {candidates.repo} in {REPO_FILE_NAME}, but "
        f"{SOURCE_LABELS[candidates.source]} says {candidates.selected}. Edit "
        f"{REPO_FILE_NAME} if the repo should use another project."
    )


def _folded(value: str) -> str:
    return trim_selector(value).casefold()


def _check_selector(value: str) -> None:
    if len(trim_selector(value)) > PROJECT_SELECTOR_MAX_LENGTH or not trim_selector(
        value
    ):
        raise ProjectResolutionError(
            "project_not_found",
            f"No project matches {trim_selector(value)[:PROJECT_SELECTOR_MAX_LENGTH]}.",
            "Project names are 1 to 100 characters; a proj_ id always works.",
        )


def _name_in_login(reads: _Reads, value: str) -> _ProjectRef:
    """A name or id through the login's ``GET /api/v1/projects``."""
    _, account_name = reads.account()
    wanted = _folded(value)
    for project in reads.projects():
        if project.name.casefold() == wanted:
            return project
    raise ProjectResolutionError(
        "project_not_found",
        f"No project named {trim_selector(value)} in {account_name}.",
        "Run solwyn projects list, or use the proj_ id.",
    )


def _local_match(
    stored: Mapping[str, auth.StoredApiKey], value: str
) -> auth.StoredApiKey:
    if is_project_id(value):
        entry = stored.get(canonical_project_id(value))
        if entry is None:
            raise api_key_not_stored(trim_selector(value))
        return entry
    wanted = _folded(value)
    matches = [
        entry for entry in stored.values() if entry.project_name.casefold() == wanted
    ]
    if len(matches) > 1:
        raise ProjectResolutionError(
            "project_ambiguous",
            f"More than one stored API key is for a project named "
            f"{trim_selector(value)} (different accounts). Use the proj_ id, or "
            "sign in with solwyn login.",
        )
    if not matches:
        raise api_key_not_stored(trim_selector(value))
    return matches[0]


class _Method:
    """One step-4 method; ``id_of`` returns None when a side cannot resolve."""

    def __init__(self, resolve: Callable[[str], str]) -> None:
        self._resolve = resolve

    def resolve(self, value: str) -> str:
        if is_project_id(value):
            return canonical_project_id(value)
        _check_selector(value)
        return self._resolve(value)

    def id_of(self, value: str) -> str | None:
        try:
            return self.resolve(value)
        except (ProjectResolutionError, ApiError, AuthError):
            return None


def _mismatch(candidates: Candidates, method: _Method, selected_id: str | None) -> bool:
    """Step 5: repo value against the flag or env value, compared by id."""
    if candidates.repo is None or candidates.source not in ("cli", "environment"):
        return False
    selected = candidates.selected or ""
    left = selected_id if selected_id is not None else method.id_of(selected)
    right = method.id_of(candidates.repo)
    if left is not None and right is not None:
        return left != right
    return _folded(selected) != _folded(candidates.repo)


def _apply_mismatch(
    candidates: Candidates, method: _Method, selected_id: str | None, purpose: Purpose
) -> str | None:
    if not _mismatch(candidates, method, selected_id):
        return None
    message = _mismatch_message(candidates)
    if purpose == "read":
        return message
    raise ProjectResolutionError("project_mismatch", message)


def _repo_error(candidates: Candidates) -> None:
    if candidates.repo_error is not None:
        raise ProjectResolutionError("repo_file_invalid", candidates.repo_error)


def _account_check(reads: _Reads, *, project: str, key_account: str | None) -> None:
    """Step 6: a stored or env key must belong to the login's account."""
    if reads.login is None:
        return
    account_id, _ = reads.account()
    if key_account != account_id:
        raise ProjectResolutionError(
            "api_key_account_mismatch",
            f"The stored API key for {project} belongs to another account than "
            "your current login. Run solwyn keys create --project "
            f"{project} --store-key.",
        )


def _stored_by_login(reads: _Reads, value: str) -> str:
    """A stored key's project id, resolved through the login (never names)."""
    if is_project_id(value):
        return canonical_project_id(value)
    return _name_in_login(reads, value).id


def _resolve_stored(
    candidates: Candidates,
    reads: _Reads,
    purpose: Purpose,
    *,
    account_check: bool,
) -> ProjectSelection:
    """``keys_env`` and the stored half of ``spend``/``read``."""
    selected = candidates.selected
    if selected is None:
        raise project_required()
    stored = auth.stored_api_keys(reads.api_url)
    if reads.login is not None:
        method = _Method(lambda value: _stored_by_login(reads, value))
        project_id = method.resolve(selected)
        warning = _apply_mismatch(candidates, method, project_id, purpose)
        entry = stored.get(project_id)
        if entry is None:
            raise api_key_not_stored(trim_selector(selected))
        if account_check:
            _account_check(
                reads, project=trim_selector(selected), key_account=entry.account_id
            )
    else:
        method = _Method(lambda value: _local_match(stored, value).project_id)
        entry = _local_match(stored, selected)
        warning = _apply_mismatch(candidates, method, entry.project_id, purpose)
    found = auth.stored_api_key(entry.project_id, reads.api_url)
    if found is None:
        raise api_key_not_stored(trim_selector(selected))
    credential = auth.stored_key_credential(*found)
    return ProjectSelection(
        selector=trim_selector(selected),
        source=candidates.source,
        project_id=entry.project_id,
        project_name=entry.project_name,
        account_id=entry.account_id,
        credential=credential,
        warning=warning,
    )


def _resolve_with_key(
    candidates: Candidates,
    reads: _Reads,
    key: ApiKeyCredential,
    purpose: Purpose,
    *,
    always_ask: bool,
    account_check: bool,
) -> ProjectSelection:
    """A key that is not stored resolves names through ``/auth/key``."""
    selected = candidates.selected
    if selected is None:
        raise project_required()
    method = _Method(lambda value: reads.auth_key(key, trim_selector(value)).project_id)
    info: AuthKeyResponse | None = None
    if always_ask or not is_project_id(selected):
        _check_selector(selected)
        info = reads.auth_key(key, trim_selector(selected))
        project_id = info.project_id
    else:
        project_id = canonical_project_id(selected)
    warning = _apply_mismatch(candidates, method, project_id, purpose)
    if account_check:
        if info is None and reads.login is not None:
            info = reads.auth_key(key, project_id)
        _account_check(
            reads,
            project=trim_selector(selected),
            key_account=None if info is None else info.account_id,
        )
    bound = key.model_copy(
        update={
            "project": project_id,
            "key_id": None if info is None else info.key_id,
            "account_id": None if info is None else info.account_id,
            "project_name": None if info is None else info.project_name,
        }
    )
    return ProjectSelection(
        selector=trim_selector(selected),
        source=candidates.source,
        project_id=project_id,
        project_name=None if info is None else info.project_name,
        account_id=None if info is None else info.account_id,
        credential=bound,
        warning=warning,
    )


def _resolve_with_login(
    candidates: Candidates, reads: _Reads, purpose: Purpose
) -> ProjectSelection:
    selected = candidates.selected
    if selected is None:
        raise project_required()
    login = reads.login
    names: dict[str, str] = {}

    def through_login(value: str) -> str:
        project = _name_in_login(reads, value)
        names[project.id] = project.name
        return project.id

    method = _Method(through_login)
    if purpose == "setup":
        # Creating or storing a key needs the account first, even for an id.
        reads.account()
    project_id = method.resolve(selected)
    warning = _apply_mismatch(candidates, method, project_id, purpose)
    return ProjectSelection(
        selector=trim_selector(selected),
        source=candidates.source,
        project_id=project_id,
        project_name=names.get(project_id),
        account_id=reads._account[0] if reads._account else None,
        credential=login,
        warning=warning,
    )


def check_setup(
    candidates: Candidates,
    *,
    api_url: str,
    credential: auth.ResolvedCredential,
) -> str | None:
    """The checks a setup command passes before it creates or writes anything.

    The repo file must be valid and must not disagree with ``--project`` or
    ``SOLWYN_PROJECT`` (compared by id through the credential, else as
    case-folded text): ``project_mismatch``. With a login, its account must be
    known (``account_unresolved`` otherwise); its id is returned for the keys
    the command will store.
    """
    _repo_error(candidates)
    login = credential if isinstance(credential, LoginCredential) else None
    reads = _Reads(api_url=api_url, login=login)
    if login is not None:
        reads.account()
        method = _Method(lambda value: _name_in_login(reads, value).id)
    elif isinstance(credential, ApiKeyCredential):
        method = _Method(
            lambda value: reads.auth_key(credential, trim_selector(value)).project_id
        )
    else:
        method = _Method(lambda value: trim_selector(value))
    _apply_mismatch(candidates, method, None, "setup")
    return None if reads._account is None else reads._account[0]


def resolve(
    candidates: Candidates,
    *,
    purpose: Purpose,
    api_url: str,
    credential: auth.ResolvedCredential | None = None,
    supplied_key: ApiKeyCredential | None = None,
    environ: Mapping[str, str] | None = None,
    factory: ClientFactory | None = None,
) -> ProjectSelection:
    """Resolve the selected project for one command (see the module docstring).

    ``read``: ``credential`` is the one today's order chose (a login, an env
    key, or None, after which a stored key is chosen here). ``spend``: the
    env key, or None for the stored key. ``with_key``: ``supplied_key``.
    """
    _repo_error(candidates)
    # A read with an env key resolves names through /auth/key and never needs
    # the login (so SOLWYN_AUTH=project_key still works with a locked
    # keychain). Every other path that may choose a stored key reads the
    # present login first (below for reads).
    login = (
        credential
        if isinstance(credential, LoginCredential)
        else auth.current_login(environ, api_url=api_url)
        if purpose in ("keys_env", "spend", "setup")
        else None
    )
    reads = _Reads(api_url=api_url, login=login)
    if factory is not None:
        reads.factory = factory
    if (
        purpose in ("read", "spend")
        and candidates.selected is None
        and isinstance(credential, ApiKeyCredential)
        and credential.project is None
    ):
        # Nothing selects a project: the key is sent without Solwyn-Project and
        # Core answers project_selector_required with its own message.
        return ProjectSelection(
            selector=None,
            source=candidates.source,
            project_id=None,
            credential=credential,
        )
    if purpose == "with_key":
        if supplied_key is None:
            raise ValueError("with_key needs the supplied key")
        reads.login = None  # login --with-key records the key's own account
        return _resolve_with_key(
            candidates,
            reads,
            supplied_key,
            purpose,
            always_ask=True,
            account_check=False,
        )
    if purpose == "setup":
        if login is None:
            raise ProjectResolutionError(
                "authentication_required",
                "This needs a Solwyn login.",
                "Run solwyn login, then retry.",
                semantic=ErrorSemantic.AUTH,
            )
        return _resolve_with_login(candidates, reads, purpose)
    if purpose == "keys_env":
        return _resolve_stored(candidates, reads, purpose, account_check=True)
    if purpose == "spend":
        if credential is None and not auth.stored_api_keys(api_url):
            # No key anywhere: the caller reports the missing credential.
            return ProjectSelection(
                selector=candidates.selected, source=candidates.source, project_id=None
            )
        if isinstance(credential, ApiKeyCredential):
            return _resolve_with_key(
                candidates,
                reads,
                credential,
                purpose,
                always_ask=reads.login is not None,
                account_check=True,
            )
        return _resolve_stored(candidates, reads, purpose, account_check=True)
    # read
    if credential is None:
        if candidates.selected is None or not auth.stored_api_keys(api_url):
            # Nothing can be chosen: the caller reports the missing credential.
            return ProjectSelection(
                selector=candidates.selected, source=candidates.source, project_id=None
            )
        # Whichever credential sends the request, a present login
        # (SOLWYN_TOKEN or the stored one, even under SOLWYN_AUTH=project_key)
        # decides how a stored key's name becomes an id: through that login's
        # account, never by matching stored names across accounts.
        reads.login = auth.current_login(environ, api_url=api_url)
        return _resolve_stored(candidates, reads, purpose, account_check=False)
    if isinstance(credential, LoginCredential):
        return _resolve_with_login(candidates, reads, purpose)
    if isinstance(credential, ApiKeyCredential):
        if credential.source != "environment" and credential.project is not None:
            return ProjectSelection(
                selector=candidates.selected,
                source=candidates.source,
                project_id=credential.project,
                project_name=credential.project_name,
                account_id=credential.account_id,
                credential=credential,
            )
        return _resolve_with_key(
            candidates,
            reads,
            credential,
            purpose,
            always_ask=False,
            account_check=False,
        )
    raise TypeError("unexpected credential")
