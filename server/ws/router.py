from __future__ import annotations

from pydantic import ValidationError

from server.types.ws import ChatWSEnvelope, WSEnvelope


def parse_ws_envelope(payload: object) -> WSEnvelope:
    if not isinstance(payload, dict):
        raise ValidationError.from_exception_data(
            "WSEnvelope",
            [
                {
                    "type": "model_type",
                    "loc": (),
                    "msg": "Input should be an object.",
                    "input": payload,
                }
            ],
        )

    if payload.get("type") == "chat":
        return ChatWSEnvelope.model_validate(payload)

    return WSEnvelope.model_validate(payload)
