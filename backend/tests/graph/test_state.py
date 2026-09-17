"""Tests for the state reducers and the pure routing helpers.

`merge_tasks` is the subtlest piece of the graph, so it gets the most coverage
here. The failure it prevents — a completed task silently reverting and being
dispatched twice — is invisible in a happy-path run and only shows up under
concurrency, which is exactly the kind of bug worth pinning with a test.
"""

from __future__ import annotations

from app.graph.state import (
    PlanTask,
    TaskStatus,
    is_plan_complete,
    merge_tasks,
    ready_tasks,
)


def task(tid: str, *, status=TaskStatus.PENDING, depends_on=None, workflow="claims"):
    return PlanTask(
        id=tid,
        workflow=workflow,
        action=f"action_{tid}",
        depends_on=depends_on or [],
        status=status,
    )


class TestMergeTasks:
    def test_empty_sides_pass_through(self):
        t = [task("1")]
        assert merge_tasks([], t) == t
        assert merge_tasks(t, []) == t

    def test_right_wins_per_task(self):
        left = [task("1", status=TaskStatus.RUNNING)]
        right = [task("1", status=TaskStatus.DONE)]
        assert merge_tasks(left, right)[0].status is TaskStatus.DONE

    def test_concurrent_delta_updates_both_survive(self):
        """Two subgraphs advancing different tasks in the same superstep.

        This is the reducer's whole job. Each node returns ONLY its own changed
        task — the documented contract — and the reducer folds them in turn, so
        both updates land.
        """
        base = [task("1", status=TaskStatus.RUNNING), task("2", status=TaskStatus.RUNNING)]
        delta_a = [task("1", status=TaskStatus.DONE)]   # onboarding finished task 1
        delta_b = [task("2", status=TaskStatus.DONE)]   # claims finished task 2

        merged = merge_tasks(merge_tasks(base, delta_a), delta_b)

        assert {t.id: t.status for t in merged} == {
            "1": TaskStatus.DONE,
            "2": TaskStatus.DONE,
        }

    def test_returning_the_whole_plan_reverts_a_concurrent_update(self):
        """Pins the failure mode the CONTRACT in merge_tasks() exists to prevent.

        Discovered by this test failing during scaffolding, which is the only
        reason the contract is written down at all.

        If a node returns the entire plan instead of its delta, every task it did
        not touch rides along as a stale write. Here node B's copy of task 1 is
        older than node A's, so task 1 reverts DONE -> RUNNING and the supervisor
        will dispatch it twice.

        Asserting the broken behaviour deliberately: it is a property of the
        calling convention, not a bug in the reducer, and if a future change
        makes this test fail the contract has been silently altered.
        """
        base = [task("1", status=TaskStatus.RUNNING), task("2", status=TaskStatus.RUNNING)]
        full_a = [task("1", status=TaskStatus.DONE), task("2", status=TaskStatus.RUNNING)]
        full_b = [task("1", status=TaskStatus.RUNNING), task("2", status=TaskStatus.DONE)]

        merged = merge_tasks(merge_tasks(base, full_a), full_b)

        assert merged[0].status is TaskStatus.RUNNING, "task 1 was reverted by a stale write"
        assert merged[1].status is TaskStatus.DONE

    def test_does_not_grow_unbounded(self):
        """operator.add would concatenate, leaving duplicate ids in conflict."""
        left = [task("1"), task("2")]
        for _ in range(5):
            left = merge_tasks(left, [task("1", status=TaskStatus.DONE)])
        assert len(left) == 2
        assert [t.id for t in left] == ["1", "2"]

    def test_preserves_left_order_so_ui_does_not_reshuffle(self):
        left = [task("1"), task("2"), task("3")]
        merged = merge_tasks(left, [task("3", status=TaskStatus.DONE), task("1")])
        assert [t.id for t in merged] == ["1", "2", "3"]

    def test_appends_newly_planned_tasks(self):
        merged = merge_tasks([task("1")], [task("1"), task("2")])
        assert [t.id for t in merged] == ["1", "2"]


class TestReadyTasks:
    def test_task_with_no_dependencies_is_ready(self):
        assert [t.id for t in ready_tasks([task("1")])] == ["1"]

    def test_blocked_until_dependency_done(self):
        plan = [task("1", status=TaskStatus.RUNNING), task("2", depends_on=["1"])]
        assert ready_tasks(plan) == []

        plan = [task("1", status=TaskStatus.DONE), task("2", depends_on=["1"])]
        assert [t.id for t in ready_tasks(plan)] == ["2"]

    def test_all_dependencies_must_be_done(self):
        plan = [
            task("1", status=TaskStatus.DONE),
            task("2", status=TaskStatus.RUNNING),
            task("3", depends_on=["1", "2"]),
        ]
        assert ready_tasks(plan) == []

    def test_terminal_tasks_are_never_redispatched(self):
        for status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED):
            assert ready_tasks([task("1", status=status)]) == []

    def test_task_awaiting_human_is_not_ready(self):
        """NEEDS_INPUT clears only via Command(resume=...), never via the supervisor."""
        assert ready_tasks([task("1", status=TaskStatus.NEEDS_INPUT)]) == []


class TestIsPlanComplete:
    def test_empty_plan_is_not_complete(self):
        assert is_plan_complete([]) is False

    def test_incomplete_while_work_remains(self):
        assert is_plan_complete([task("1", status=TaskStatus.DONE), task("2")]) is False

    def test_complete_on_partial_success(self):
        """A failed task with skipped dependents is still a finished plan.

        Partial success is the normal case in operations work, so composing the
        final response must not assume everything succeeded.
        """
        plan = [
            task("1", status=TaskStatus.DONE),
            task("2", status=TaskStatus.FAILED),
            task("3", status=TaskStatus.SKIPPED),
        ]
        assert is_plan_complete(plan) is True
