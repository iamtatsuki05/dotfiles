from __future__ import annotations

import inspect
import unittest

import agent_team.verification_gate as gate
from agent_team.verification_gate import (
    ApprovalAdmissionPort,
    ApprovalRef,
    VerificationGate,
    VerificationStatePort,
)


class DurableSeamRedTest(unittest.TestCase):
    def test_public_gate_accepts_only_opaque_approval_ref_and_resume_handle(
        self,
    ) -> None:
        self.assertTrue(ApprovalAdmissionPort)
        self.assertTrue(VerificationStatePort)
        self.assertEqual(
            tuple(inspect.signature(VerificationGate.start).parameters),
            ("self", "approval_ref"),
        )
        self.assertEqual(
            tuple(inspect.signature(VerificationGate.resume).parameters),
            ("self", "handle"),
        )
        self.assertFalse(hasattr(VerificationGate, "execute"))
        for internal_name in (
            "VerificationRequest",
            "VerificationReceipt",
            "VerificationEvidence",
            "VerificationDurableRecord",
        ):
            self.assertNotIn(internal_name, gate.__all__)
        self.assertIsInstance(ApprovalRef("opaque-approval"), str)
