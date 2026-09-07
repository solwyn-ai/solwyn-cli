"""Local typed declarations, independent of SDK convenience-layer internals."""

from collections.abc import Awaitable, Callable
import inspect
import json
from typing import Any, get_type_hints

from mcp.types import Tool
from pydantic import BaseModel, ConfigDict, create_model


class ToolDeclaration(BaseModel):
    """Keep a tool's callable and its runtime/published models together."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    fn: Callable[..., Awaitable[BaseModel]]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    definition: Tool

    @classmethod
    def from_function(cls, fn, *, name: str, description: str) -> "ToolDeclaration":
        hints = get_type_hints(fn, include_extras=True)
        fields = {
            parameter.name: (
                hints[parameter.name],
                ...
                if parameter.default is inspect.Parameter.empty
                else parameter.default,
            )
            for parameter in inspect.signature(fn).parameters.values()
        }
        inputs = create_model(
            f"{fn.__name__}Arguments",
            __config__=ConfigDict(extra="forbid", hide_input_in_errors=True),
            **fields,
        )
        output = hints["return"]
        return cls(
            fn=fn,
            input_model=inputs,
            output_model=output,
            definition=Tool(
                name=name,
                description=description,
                input_schema=inputs.model_json_schema(by_alias=True),
                output_schema=output.model_json_schema(by_alias=True),
            ),
        )

    def parse_json_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Preserve 1.x JSON object/list/null preparsing and scalar coercions.

        Plain string fields stay verbatim. Non-string fields may arrive as JSON
        strings from older hosts; decoded scalar strings/numbers remain verbatim
        for Pydantic's existing coercion rules. Booleans are numbers in Python,
        matching the prior SDK behavior. Unknown keys still reach extra=forbid.
        """
        parsed = arguments.copy()
        for name, value in arguments.items():
            field = self.input_model.model_fields.get(name)
            if field is None or field.annotation is str or not isinstance(value, str):
                continue
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                continue
            if not isinstance(decoded, str | int | float):
                parsed[name] = decoded
        return parsed
