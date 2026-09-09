from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from typing import cast

from agent_team.task_spec import (
    TaskSpec,
    TaskSpecValidationError,
    VerificationSpec,
    parse_task_specs,
    task_schema,
)


def verification() -> VerificationSpec:
    return VerificationSpec(
        name="unit",
        argv=("/usr/bin/python3", "-m", "unittest", "tests/test_task_spec.py"),
        timeout_seconds=120,
    )


def sample_task() -> TaskSpec:
    return TaskSpec(
        task_id="wire-task",
        objective="Connect the structured task request to the native runner.",
        acceptance_criteria=("The native assignment retains the task identity.",),
        allowed_paths=("agent_team/task_spec.py", "tests/"),
        forbidden_paths=("upstreams.json",),
        dependencies=("previous-task",),
        verification=(verification(),),
        evidence_requirements=("Report the exact test command and result.",),
        consultation_conditions=("Ask before changing an upstream-managed path.",),
    )


class TaskSpecTest(unittest.TestCase):
    def test_parse_task_specs_preserves_declaration_order_and_allows_empty(
        self,
    ) -> None:
        first = sample_task().as_dict()
        first["task_id"] = "first-task"
        first["dependencies"] = []
        second = sample_task().as_dict()
        second["task_id"] = "second-task"
        second["dependencies"] = ["first-task"]

        self.assertEqual(parse_task_specs([]), ())
        parsed = parse_task_specs([first, second])

        self.assertEqual(
            tuple(item.task_id for item in parsed), ("first-task", "second-task")
        )

    def test_parse_task_specs_rejects_non_serialized_or_non_catalog_values(
        self,
    ) -> None:
        encoded = sample_task().as_dict()
        values: tuple[object, ...] = (None, {}, sample_task(), "tasks")
        for value in values:
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(TaskSpecValidationError),
            ):
                parse_task_specs(value)
        with self.assertRaises(TaskSpecValidationError):
            parse_task_specs([encoded, {**encoded}])

    def test_parse_task_specs_rejects_unknown_dependencies_and_cycles(self) -> None:
        unknown = sample_task().as_dict()
        unknown["dependencies"] = ["missing-task"]
        with self.assertRaises(TaskSpecValidationError):
            parse_task_specs([unknown])

        first = sample_task().as_dict()
        first["task_id"] = "first-task"
        first["dependencies"] = ["second-task"]
        second = sample_task().as_dict()
        second["task_id"] = "second-task"
        second["dependencies"] = ["first-task"]
        with self.assertRaises(TaskSpecValidationError):
            parse_task_specs([first, second])

    def test_round_trip_is_immutable_and_preserves_directory_suffix(self) -> None:
        task = sample_task()

        encoded = task.as_dict()
        decoded = TaskSpec.from_dict(encoded)

        self.assertEqual(decoded, task)
        self.assertEqual(
            encoded["allowed_paths"], ["agent_team/task_spec.py", "tests/"]
        )
        with self.assertRaises(FrozenInstanceError):
            task.objective = "changed"  # type: ignore[misc]

    def test_forbidden_path_can_refine_an_allowed_path(self) -> None:
        encoded = sample_task().as_dict()
        encoded["allowed_paths"] = ["src/"]
        encoded["forbidden_paths"] = ["src/"]

        task = TaskSpec.from_dict(encoded)

        self.assertEqual(task.allowed_paths, ("src/",))
        self.assertEqual(task.forbidden_paths, ("src/",))

    def test_schema_is_exact_and_requires_every_field(self) -> None:
        schema = task_schema()

        self.assertEqual(
            set(cast(list[str], schema["required"])),
            {
                "task_id",
                "objective",
                "acceptance_criteria",
                "allowed_paths",
                "forbidden_paths",
                "dependencies",
                "verification",
                "evidence_requirements",
                "consultation_conditions",
            },
        )
        self.assertFalse(schema["additionalProperties"])
        properties = cast(dict[str, object], schema["properties"])
        self.assertEqual(
            cast(dict[str, object], properties["objective"])["pattern"],
            r".*\S.*",
        )
        verification_schema = cast(dict[str, object], schema["properties"])[
            "verification"
        ]
        self.assertEqual(
            cast(dict[str, object], verification_schema)["minItems"],
            1,
        )

    def test_from_dict_rejects_unknown_missing_and_duplicate_keys(self) -> None:
        encoded = sample_task().as_dict()

        with self.assertRaises(TaskSpecValidationError):
            TaskSpec.from_dict({**encoded, "unexpected": True})
        missing = dict(encoded)
        del missing["objective"]
        with self.assertRaises(TaskSpecValidationError):
            TaskSpec.from_dict(missing)

        class DuplicateMapping(dict[str, object]):
            def items(self):  # type: ignore[no-untyped-def]
                return tuple(super().items()) + (("objective", "duplicate"),)

        with self.assertRaises(TaskSpecValidationError):
            TaskSpec.from_dict(DuplicateMapping(encoded))

    def test_direct_construction_rejects_malformed_scalars_and_paths(self) -> None:
        base = sample_task()
        cases: tuple[dict[str, object], ...] = (
            {"task_id": "Bad ID"},
            {"objective": "has\x00nul"},
            {"objective": "   "},
            {"acceptance_criteria": ("\t",)},
            {"acceptance_criteria": ()},
            {"allowed_paths": ("../outside",)},
            {"allowed_paths": ("src/*.py",)},
            {"allowed_paths": ("src/", "src/")},
            {"forbidden_paths": ("/absolute",)},
            {"forbidden_paths": ("src/", "src/")},
            {"dependencies": ["mutable-list"]},
            {"dependencies": ("previous-task", "previous-task")},
            {"dependencies": ("wire-task",)},
            {"verification": ()},
            {"verification": (verification(), verification())},
            {"evidence_requirements": ()},
        )
        for changes in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaises(TaskSpecValidationError),
            ):
                values: dict[str, object] = {
                    "task_id": base.task_id,
                    "objective": base.objective,
                    "acceptance_criteria": base.acceptance_criteria,
                    "allowed_paths": base.allowed_paths,
                    "forbidden_paths": base.forbidden_paths,
                    "dependencies": base.dependencies,
                    "verification": base.verification,
                    "evidence_requirements": base.evidence_requirements,
                    "consultation_conditions": base.consultation_conditions,
                }
                values.update(changes)
                TaskSpec(**values)  # type: ignore[arg-type]

    def test_verification_requires_fixed_nonempty_argv_and_bounded_timeout(
        self,
    ) -> None:
        self.assertEqual(
            VerificationSpec(
                name="unit", argv=("python", "  "), timeout_seconds=1
            ).argv[1],
            "  ",
        )
        with self.assertRaises(TaskSpecValidationError):
            VerificationSpec(name="unit", argv=(), timeout_seconds=1)
        with self.assertRaises(TaskSpecValidationError):
            VerificationSpec(name="unit", argv=("python",), timeout_seconds=901)
        with self.assertRaises(TaskSpecValidationError):
            VerificationSpec(name="unit", argv=("python\x00",), timeout_seconds=1)
        with self.assertRaises(TaskSpecValidationError):
            VerificationSpec(name="unit", argv=("python",), timeout_seconds=True)


if __name__ == "__main__":
    unittest.main()
