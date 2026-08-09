from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

RepoName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
]
TargetName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$")]
Login = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"),
]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
Sha = Annotated[str, StringConstraints(min_length=7)]
SourceId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+#[1-9][0-9]*$")]
PullRequestRef = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$"),
]
FindingId = Annotated[str, StringConstraints(pattern=r"^AGR-[0-9]{4}-[0-9]{3,}$")]
SemanticFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^semantic:[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$"),
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Automation(StrEnum):
    PR = "pr"
    NOTIFY = "notify"


class Changelog(StrEnum):
    KEEP_A_CHANGELOG = "keep-a-changelog"
    FRAGMENT = "fragment"
    NONE = "none"


class DecisionKind(StrEnum):
    PROPAGATE = "propagate"
    SKIP = "skip"


class PropagationOutcomeKind(StrEnum):
    PUBLISHED = "published"
    SKIPPED = "skipped"


class LabelAction(StrEnum):
    LABELED = "labeled"
    UNLABELED = "unlabeled"


class Repository(FrozenModel):
    repo: RepoName


class SDK(FrozenModel):
    repo: RepoName
    automation: Automation
    owners: tuple[Login, ...]
    changelog: Changelog
    verify: tuple[NonEmpty, ...]

    @model_validator(mode="after")
    def require_pr_verification(self) -> SDK:
        if self.automation is Automation.PR and not self.verify:
            raise ValueError("verify must not be empty for pr automation")
        return self


class Manifest(FrozenModel):
    version: Literal[1]
    maintainers: frozenset[Login] = Field(min_length=1)
    canonical: Repository
    spec: Repository
    sdks: Mapping[TargetName, SDK] = Field(min_length=1)

    @field_validator("sdks", mode="after")
    @classmethod
    def freeze_sdks(cls, value: Mapping[str, SDK]) -> Mapping[str, SDK]:
        return MappingProxyType(dict(value))

    @field_serializer("sdks")
    def serialize_sdks(self, value: Mapping[str, SDK]) -> dict[str, SDK]:
        return dict(value)

    @model_validator(mode="after")
    def require_unique_repositories(self) -> Manifest:
        repositories = [
            self.canonical.repo,
            self.spec.repo,
            *(sdk.repo for sdk in self.sdks.values()),
        ]
        if len(repositories) != len(set(repositories)):
            duplicate = next(
                repo for repo in repositories if repositories.count(repo) > 1
            )
            raise ValueError(f"repository appears more than once: {duplicate}")
        return self

    def target(self, name: str) -> SDK:
        try:
            return self.sdks[name]
        except KeyError as exc:
            raise ValueError(f"unknown SDK target: {name}") from exc

    def pr_targets(self) -> tuple[str, ...]:
        return tuple(
            name for name, sdk in self.sdks.items() if sdk.automation is Automation.PR
        )


class Source(FrozenModel):
    repo: RepoName
    pr: PositiveInt
    sha: Sha


class AuditSource(FrozenModel):
    repo: RepoName
    sha: Sha
    finding: FindingId
    fingerprint: NonEmpty


class AuditTarget(FrozenModel):
    repo: RepoName
    sha: Sha


class AuditFindingContext(FrozenModel):
    id: FindingId
    fingerprint: NonEmpty
    canonical: AuditTarget
    affected: Mapping[TargetName, AuditTarget] = Field(min_length=1)

    @field_validator("affected", mode="after")
    @classmethod
    def freeze_affected(
        cls, value: Mapping[str, AuditTarget]
    ) -> Mapping[str, AuditTarget]:
        return MappingProxyType(dict(value))

    @field_serializer("affected")
    def serialize_affected(
        self, value: Mapping[str, AuditTarget]
    ) -> dict[str, AuditTarget]:
        return dict(value)


class DecisionBase(FrozenModel):
    target: TargetName
    by: Login
    idempotency_key: NonEmpty
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)


class PropagateDecision(DecisionBase):
    decision: Literal[DecisionKind.PROPAGATE]
    pr: PullRequestRef
    reason: None = None


class SkipDecision(DecisionBase):
    decision: Literal[DecisionKind.SKIP]
    reason: NonEmpty
    pr: None = None


Decision = Annotated[PropagateDecision | SkipDecision, Field(discriminator="decision")]


class LedgerEntry(FrozenModel):
    id: SourceId
    source: Source
    labels: tuple[str, ...]
    decisions: tuple[Decision, ...] = ()

    @model_validator(mode="after")
    def validate_identity_and_idempotency(self) -> LedgerEntry:
        expected_id = f"{self.source.repo.rsplit('/', 1)[-1]}#{self.source.pr}"
        if self.id != expected_id:
            raise ValueError(f"id must be {expected_id}")
        keys = [decision.idempotency_key for decision in self.decisions]
        if len(keys) != len(set(keys)):
            raise ValueError("decision idempotency keys must be unique")
        return self


@dataclass(frozen=True)
class PullRequestFile:
    path: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class CanonicalChange:
    repo: str
    number: int
    sha: str
    title: str
    url: str
    body: str
    merged_at: datetime
    labels: tuple[str, ...] = ()
    files: tuple[PullRequestFile, ...] = ()

    @property
    def source_id(self) -> str:
        return f"{self.repo.rsplit('/', 1)[-1]}#{self.number}"

    @property
    def marker(self) -> str:
        return f"<!-- agricola:source={self.repo}#{self.number} -->"


@dataclass(frozen=True)
class LabelEvent:
    action: LabelAction
    label: str
    actor: str
    created_at: datetime


@dataclass(frozen=True)
class LabelResolution:
    labels: tuple[str, ...]
    targets: tuple[str, ...]
    affected: tuple[str, ...] = ()
    target_actors: tuple[tuple[str, str], ...] = ()
    disabled: bool = False
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def actor_for(self, target: str) -> str:
        try:
            return dict(self.target_actors)[target]
        except KeyError as exc:
            raise ValueError(f"missing authorized actor for target: {target}") from exc


class CommandVerb(StrEnum):
    PLAN = "plan"
    PROPAGATE = "propagate"
    STATUS = "status"
    SKIP = "skip"


@dataclass(frozen=True)
class Command:
    verb: CommandVerb
    target: str | None = None
    targets: tuple[str, ...] = ()
    all_targets: bool = False
    instruction: str | None = None
    reason: str | None = None
    line: int = 1


class PropagationRevision(FrozenModel):
    pr: PullRequestRef
    url: NonEmpty
    head_sha: Sha


class PropagationRequest(FrozenModel):
    source: Source | AuditSource
    source_title: NonEmpty
    source_url: NonEmpty
    target: TargetName
    target_repo: RepoName
    target_base_sha: Sha
    tracking_issue: PositiveInt
    tracking_issue_url: NonEmpty
    by: Login
    idempotency_key: NonEmpty
    branch: NonEmpty
    verify: tuple[NonEmpty, ...]
    changelog: Changelog
    owners: tuple[Login, ...] = ()
    plan: NonEmpty
    instruction: NonEmpty | None = None
    revision: PropagationRevision | None = None

    @model_validator(mode="after")
    def require_revision_instruction_and_head(self) -> PropagationRequest:
        if self.revision is None:
            return self
        if self.instruction is None:
            raise ValueError("revision requires an instruction")
        if self.target_base_sha != self.revision.head_sha:
            raise ValueError("revision head must equal target_base_sha")
        return self


class PropagationResult(FrozenModel):
    outcome: Literal[PropagationOutcomeKind.PUBLISHED] = (
        PropagationOutcomeKind.PUBLISHED
    )
    request: PropagationRequest
    pr: PullRequestRef
    url: NonEmpty
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_target_repository(self) -> PropagationResult:
        repository = self.pr.rsplit("#", 1)[0]
        if repository != self.request.target_repo:
            raise ValueError(f"pr must belong to {self.request.target_repo}")
        return self


class PropagationSkip(FrozenModel):
    outcome: Literal[PropagationOutcomeKind.SKIPPED] = PropagationOutcomeKind.SKIPPED
    request: PropagationRequest
    reason: NonEmpty
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def forbid_revision_skip(self) -> PropagationSkip:
        if self.request.revision is not None:
            raise ValueError("revision cannot be skipped")
        return self


PropagationOutcome = Annotated[
    PropagationResult | PropagationSkip,
    Field(discriminator="outcome"),
]


class PendingReply(FrozenModel):
    issue_number: PositiveInt
    body: NonEmpty


class PendingIssueUpdate(FrozenModel):
    issue_number: PositiveInt
    body: NonEmpty


class AuditCheckStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class AuditSeverity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AuditConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AuditObservation(FrozenModel):
    fingerprint: NonEmpty
    status: AuditCheckStatus
    summary: NonEmpty
    reference: str | None = None


class AuditCodeEvidence(FrozenModel):
    path: NonEmpty
    line: PositiveInt | None
    symbol: str | None
    behavior: NonEmpty

    @field_validator("path")
    @classmethod
    def require_repository_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or path.parts[0] == ".audit"
        ):
            raise ValueError("path must be relative to the audited repository")
        return value


class AuditSemanticFinding(FrozenModel):
    fingerprint: SemanticFingerprint
    title: NonEmpty
    description: NonEmpty
    severity: AuditSeverity
    confidence: AuditConfidence
    canonical: AuditCodeEvidence
    target: AuditCodeEvidence
    spec_reference: str | None
    suggested_test: NonEmpty


class AuditSemanticResult(FrozenModel):
    schema_version: Literal[1]
    target: TargetName
    canonical_sha: Sha
    target_sha: Sha
    summary: NonEmpty
    findings: tuple[AuditSemanticFinding, ...]

    @model_validator(mode="after")
    def require_unique_findings(self) -> AuditSemanticResult:
        fingerprints = [finding.fingerprint for finding in self.findings]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("semantic finding fingerprints must be unique")
        return self


class AuditSnapshot(FrozenModel):
    target: TargetName
    repo: RepoName
    sha: Sha
    capabilities: frozenset[NonEmpty]
    observations: tuple[AuditObservation, ...]
    semantic_reviewed: bool = False
    semantic_summary: NonEmpty | None = None
    semantic_findings: tuple[AuditSemanticFinding, ...] = ()
    semantic_error: NonEmpty | None = None
    errors: tuple[NonEmpty, ...] = ()

    @model_validator(mode="after")
    def require_unique_observations(self) -> AuditSnapshot:
        fingerprints = [item.fingerprint for item in self.observations]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("audit observation fingerprints must be unique")
        semantic_fingerprints = [
            finding.fingerprint for finding in self.semantic_findings
        ]
        if len(semantic_fingerprints) != len(set(semantic_fingerprints)):
            raise ValueError("semantic finding fingerprints must be unique")
        if self.semantic_reviewed != (self.semantic_summary is not None):
            raise ValueError("completed semantic review requires a summary")
        if self.semantic_findings and not self.semantic_reviewed:
            raise ValueError("semantic findings require a completed review")
        if self.semantic_reviewed and self.semantic_error is not None:
            raise ValueError("completed semantic review cannot contain an error")
        return self


class AuditFindingRecord(FrozenModel):
    id: FindingId
    fingerprint: NonEmpty
    first_seen: datetime

    @field_validator("first_seen")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)


class AuditRegistry(FrozenModel):
    version: Literal[1] = 1
    findings: tuple[AuditFindingRecord, ...] = ()

    @model_validator(mode="after")
    def require_unique_findings(self) -> AuditRegistry:
        ids = [finding.id for finding in self.findings]
        fingerprints = [finding.fingerprint for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("audit finding IDs must be unique")
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("audit finding fingerprints must be unique")
        return self


class AuditSemanticEvidence(FrozenModel):
    target: TargetName
    finding: AuditSemanticFinding


class AuditFinding(FrozenModel):
    id: FindingId
    fingerprint: NonEmpty
    summary: NonEmpty
    reference: str | None = None
    affected: tuple[NonEmpty, ...] = Field(min_length=1)
    clean: tuple[NonEmpty, ...] = ()
    likely_origin: NonEmpty
    severity: AuditSeverity | None = None
    confidence: AuditConfidence | None = None
    semantic_evidence: tuple[AuditSemanticEvidence, ...] = ()


class AuditReport(FrozenModel):
    generated_at: datetime
    canonical: AuditSnapshot
    targets: tuple[AuditSnapshot, ...]
    findings: tuple[AuditFinding, ...]
    errors: tuple[NonEmpty, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)


class PendingAuditFindingIssue(FrozenModel):
    id: FindingId
    marker: NonEmpty
    title: NonEmpty
    body: NonEmpty
    labels: tuple[NonEmpty, ...] = ("agricola",)


class PendingAuditReport(FrozenModel):
    title: NonEmpty
    body: NonEmpty
    healthy: bool
    finding_issues: tuple[PendingAuditFindingIssue, ...] = ()


class Cursor(FrozenModel):
    merged_at: datetime

    @field_validator("merged_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    def advance(self, change: CanonicalChange) -> Cursor:
        return Cursor(merged_at=max(self.merged_at, change.merged_at))
