"""Shared field types. Closed sets use Literal (JSON Schema enum); free text declares its encoding."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

PLAIN = {"x-datumbridge-encoding": "plain"}

PlainText = Annotated[str, Field(json_schema_extra=PLAIN)]

# A Word/PowerPoint table cell: text, or a number/boolean/null rendered as text.
TableCell = Annotated[str | int | float | bool | None, Field(json_schema_extra=PLAIN)]


class StrictModel(BaseModel):
    """Unknown keys are rejected at the MCP layer (JSON-RPC -32602), not silently dropped."""

    model_config = ConfigDict(extra="forbid")


def cell_text(value: str | int | float | bool | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
