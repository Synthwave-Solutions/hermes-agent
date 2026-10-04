"""Route the SDK raw-response call to the test's ``create`` mock (SYNTHWAVE fork).

SynthPulse sends non-streaming chat completions through
``client.chat.completions.with_raw_response.create`` so it can read OmniRoute's
routing headers. Tests that script ``client.chat.completions.create`` would
otherwise receive a bare MagicMock from the raw path ("Object of type
MagicMock is not JSON serializable"). The helper also keeps the mock from
posing as the MoA facade, which real SDK clients never are.
"""

from unittest.mock import MagicMock


def wire_raw_response(client):
    """Make ``with_raw_response.create`` return ``create``'s scripted result."""

    def raw_create(**kwargs):
        parsed = client.chat.completions.create(**kwargs)
        raw = MagicMock()
        raw.headers = {}
        raw.parse.return_value = parsed
        return raw

    client.chat.completions.with_raw_response.create.side_effect = raw_create
    # A plain SDK client is not the MoA facade. Without this, MagicMock
    # answers every duck-typed MoA probe (aggregator slot for pricing,
    # reference usage) with more mocks, which then reach JSON encoding.
    client.last_aggregator_slot = None
    for name in ("consume_reference_usage", "consume_and_save_trace"):
        try:
            delattr(client, name)
        except AttributeError:
            pass
    return client
