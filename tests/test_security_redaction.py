from codexthrift.security.redaction import redact_sensitive_text, redact_sensitive_value


def test_redaction_masks_credentials_and_contact_information() -> None:
    value = {
        "password": "private-password",
        "cookie": "session=private",
        "email": "owner@example.com",
        "phone": "+1 416 555 0199",
        "address": "100 Private Street",
        "message": "Contact owner@example.com or 416-555-0199 with Bearer abcdefghijklmnop.",
    }

    redacted = redact_sensitive_value(value)

    assert redacted["password"] == "***REDACTED***"
    assert redacted["cookie"] == "***REDACTED***"
    assert redacted["email"] == "***REDACTED***"
    assert redacted["phone"] == "***REDACTED***"
    assert redacted["address"] == "***REDACTED***"
    assert "owner@example.com" not in redacted["message"]
    assert "416-555-0199" not in redacted["message"]
    assert "Bearer abcdefghijklmnop" not in redacted["message"]


def test_redaction_preserves_token_usage_metrics() -> None:
    assert redact_sensitive_text("input_tokens=123") == "input_tokens=123"
    assert redact_sensitive_value({"input_tokens": 123, "output_tokens": 4}) == {
        "input_tokens": 123,
        "output_tokens": 4,
    }
