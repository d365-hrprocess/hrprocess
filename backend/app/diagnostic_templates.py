"""Value-free provider message templates, not a best-effort PII scrubber.

Only a closed technical vocabulary survives. Quoted strings, URLs, addresses,
opaque identifiers and unknown words become markers. No original free text is
retained. This gives unfamiliar errors useful structure without wire logging.
"""

import re

# Words describe failure mechanisms, not candidate, business or credential values.
_WORDS = frozenset(
    """set a an the and or of to for from in on at with without by is are was were be been being not no cannot could failed failure error exception occurred occured when invoking operation request response required missing mandatory field fields table record records value values length maximum minimum exceeds exceeded limit size string integer decimal boolean enum symbol type types invalid valid validation validate assignment conversion convert cast argument arguments parameter parameters method object reference instance null empty initialized initialise initialize create creating update updating insert inserting delete deleting read reading write writing database transaction committed commit aborted abort rollback cancelled canceled constraint duplicate key unique index already exists conflict permission denied access unauthorized authentication authorization service unavailable server internal timeout timed out connection connect disconnected network certificate expired trust hostname resolve resolution serialization deserialization deserialize serialize json xml format syntax unexpected supported unsupported found expected existing current company legal entity integration user configured matches match mismatch configuration business default defaults endpoint resource routing route token expired refresh grant scope client secret incorrect operationid activityid correlationid code message detail details innerexception innererror exceptiontype stacktrace synchronous asynchronous execution execute executing sql clr managed infolog warning enabled disabled number numeric data truncation truncated overflow underflow disposed locked deadlock concurrency concurrencyexception throttled throttling too many requests retry retrying try again later message statement cancelled permissions necessary initialized invalidoperationexception argumentexception nullreferenceexception errorexception targetinvocationexception cannotcreate cannotupdate serviceoperation custom physical schema column columns associated relation relations related status pending completed identity application candidate evaluation requirement parameters supplied expected received result results returned return responsebody does do did has have had must should only allow allows allowed change changed external id would will can because due date precision scale failed. reached unhandled invalidcastexception formatException stringsize extendeddatatype ttsbegin ttscommit ax dynamics microsoft system newtonsoft jsonserializationexception""".lower().split()
)
# Never preserve request values even if they happen to be an English word.
_QUOTED = re.compile(r"""["'][^"'\r\n]*["']""")
_OPAQUE = re.compile(r"https?://\S+|\b\S+@\S+\b|\b(?:[A-Za-z0-9_]+[-/.:=][A-Za-z0-9_./:=+-]+)\b", re.I)
_TOKEN = re.compile(r"[A-Za-z]+|\[[a-z]+\]|[^\w\s]", re.ASCII)


def safe_message_template(message: str, *, sensitive_values=()) -> str:
    if not isinstance(message, str):
        return "[redacted]"
    text = message[:4096]
    for value in sensitive_values:
        if isinstance(value, str) and value and len(value) <= len(text):
            text = re.sub(re.escape(value), "[redacted]", text, flags=re.I)
    text = _QUOTED.sub(" [redacted] ", text)
    text = _OPAQUE.sub(" [redacted] ", text)
    text = re.sub(r"\d+|_", " [redacted] ", text)
    tokens = []
    for token in _TOKEN.findall(text):
        value = token.lower()
        safe = value if value in _WORDS else "[redacted]"
        if safe == "[redacted]" and tokens and tokens[-1] == safe:
            continue
        tokens.append(safe)
        if len(tokens) >= 64:
            break
    return " ".join(tokens)[:512] or "[redacted]"


def valid_message_template(value):
    return (
        isinstance(value, str)
        and len(value) <= 512
        and all(token == "[redacted]" or token in _WORDS for token in value.split())
    )
