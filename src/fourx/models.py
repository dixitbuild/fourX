"""Plain, JSON-compatible contracts shared by tools, storage, providers and the runner."""
from typing import Any, Literal, NotRequired, TypedDict


PROVIDER_NAMES = ("gemini", "groq", "openai")


class ToolDefinition(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(TypedDict):
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolResult(TypedDict):
    call_id: str
    name: str
    status: Literal["completed", "error", "refused", "declined", "cancelled"]
    output: Any


class Message(TypedDict):
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: NotRequired[list[ToolCall]]
    tool_result: NotRequired[ToolResult]
    provider_metadata: NotRequired[dict[str, Any]]


class Vendor(TypedDict):
    vendor_id: str
    name: str
    status: Literal["active", "blocked"]
    payment_terms_days: NotRequired[int]
    blocked_reason: NotRequired[str]


class PostingPayload(TypedDict):
    vendor_id: str
    invoice_number: str
    invoice_date: str
    currency: str
    amount: str


class Proposal(TypedDict):
    """Exactly what the clerk approves; built from files on disk, never from model output."""
    invoice_id: str
    invoice: dict[str, Any]
    vendor: Vendor
    posting: PostingPayload


class Posting(TypedDict):
    action_id: str
    conversation_id: str
    call_id: str
    proposal: Proposal
    reference: str
    # awaiting_approval -> approved (intent saved) -> posted | failed | conflict
    # awaiting_approval -> declined | refused; a lost "approved" goes back to awaiting_approval
    state: Literal["awaiting_approval", "approved", "posted", "declined", "refused", "failed", "conflict"]
    entry_id: str | None


class LedgerEntry(PostingPayload):
    entry_id: str
    reference: str | None
    posted_at: str
