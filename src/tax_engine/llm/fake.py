"""Deterministic fake LLM for commentary.

Writes the note from the structured payload. A configurable share of notes "round" one
figure (for example "about $12,000"), the failure mode the grounding check exists to catch.
"""

from __future__ import annotations

import hashlib
import re

from tax_engine.llm import LLMRequest, LLMResponse


class FakeLLM:
    name = "fake"

    def __init__(self, drift: float = 0.05) -> None:
        self.drift = drift

    def complete(self, request: LLMRequest) -> LLMResponse:
        p = request.payload
        net = p["figures"]["net_payable"]
        position = (
            f"The net amount payable for {p['quarter']} is {net}"
            if not net.startswith("-")
            else f"{p['quarter']} is a refund position of {net.lstrip('-')}"
        )
        sentences = [
            f"{position}, from tax on sales of {p['figures']['output_tax']} less "
            f"credits of {p['figures']['input_tax']} plus {p['figures']['wages_withheld']}"
            f" withheld from wages."
        ]
        if p["change"] != "not available":
            sentences.append(f"That is a change of {p['change']} on the prior quarter.")
        if p["findings"]:
            top = p["findings"][0]
            sentences.append(f"The main item to check is {top['title'].lower()}: {top['message']}")
        else:
            sentences.append("No validation findings were raised.")
        text = " ".join(sentences)
        digest = hashlib.sha256(f"{p['tenant_id']}|{p['quarter']}".encode()).digest()
        if int.from_bytes(digest[:4], "big") / 2**32 < self.drift:
            # Simulated model error: rounds the headline figure.
            amount = float(re.sub(r"[^\d.]", "", net))
            text = text.replace(net.lstrip("-"), f"about ${round(amount, -3):,.0f}", 1)
        return LLMResponse(
            text=text,
            model=f"fake:{request.model}",
            input_tokens=max(1, len(request.system + request.user) // 4),
            output_tokens=max(1, len(text) // 4),
        )
