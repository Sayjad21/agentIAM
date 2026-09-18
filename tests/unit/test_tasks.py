"""The console's task driver — `agentiam_controlplane.tasks`.

Two properties are worth pinning here, and they are the two a demo would quietly lose:
that a ceiling typed into the form actually binds the minted token, and that a spawn
cannot hand a child more than its parent held. Everything else on the `/run` page is
form plumbing that a browser pass checks better than a unit test would.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agentiam_controlplane.tasks import DEMO_MANDATE_ID, TaskRegistry, TaskRegistryError
from agentiam_core.tokens import RootKeySet, generate_keypair, verify

_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def registry() -> TaskRegistry:
    """A registry on a throwaway keypair, with a frozen clock."""
    keys = generate_keypair()
    return TaskRegistry(
        root_private_key=keys.private_key,
        root_public_key=keys.public_key,
        now=lambda: _NOW,
    )


@pytest.fixture
def key_set(registry: TaskRegistry) -> RootKeySet:
    """The public half, for reading back what was minted."""
    return RootKeySet([registry._public_key])


class TestCreateTask:
    """What the operator types has to reach the token."""

    def test_the_ceiling_typed_into_the_form_binds_the_token(
        self, registry: TaskRegistry, key_set: RootKeySet
    ) -> None:
        """The typed ceiling reaches the token.

        The number is the whole point of the page — a demo where it is decorative is
        worse than no demo, because it invites a claim the system cannot keep.
        """
        task = registry.create_task(
            description="Procure 500 units of packaging stock.",
            budget=Decimal("50000"),
            scopes=frozenset({"invoice:read", "payment:initiate"}),
            principal_id="kc:alice",
        )
        verified = verify(task.root.token, key_set, now=_NOW)
        assert verified.budget.spend_bdt == Decimal("50000")
        assert verified.scopes == {"invoice:read", "payment:initiate"}

    def test_the_mandate_is_the_one_the_pep_is_bound_to(
        self, registry: TaskRegistry, key_set: RootKeySet
    ) -> None:
        """The task id varies; the mandate does not.

        `LeasePool` binds one `mandate_id` at construction (gap 25). A task minted under
        a fresh mandate would have no pool behind it and every budgeted call would fail.
        """
        task = registry.create_task(
            description="anything",
            budget=Decimal("1000"),
            scopes=frozenset({"invoice:read"}),
            principal_id="kc:alice",
        )
        verified = verify(task.root.token, key_set, now=_NOW)
        assert verified.mandate_id == DEMO_MANDATE_ID
        assert verified.task_id == task.task_id

    def test_two_tasks_get_different_task_ids(self, registry: TaskRegistry) -> None:
        """Otherwise the identity tree would merge them into one graph."""
        first = registry.create_task(
            description="one",
            budget=Decimal("100"),
            scopes=frozenset({"invoice:read"}),
            principal_id="kc:alice",
        )
        second = registry.create_task(
            description="two",
            budget=Decimal("100"),
            scopes=frozenset({"invoice:read"}),
            principal_id="kc:alice",
        )
        assert first.task_id != second.task_id

    @pytest.mark.parametrize(
        ("description", "budget", "scopes"),
        [
            ("", Decimal("100"), frozenset({"invoice:read"})),
            ("   ", Decimal("100"), frozenset({"invoice:read"})),
            ("ok", Decimal("0"), frozenset({"invoice:read"})),
            ("ok", Decimal("-1"), frozenset({"invoice:read"})),
            ("ok", Decimal("100"), frozenset()),
            ("ok", Decimal("100"), frozenset({"not:a:real:scope"})),
        ],
    )
    def test_a_malformed_task_is_refused_rather_than_minted(
        self, registry: TaskRegistry, description: str, budget: Decimal, scopes: frozenset[str]
    ) -> None:
        """A malformed task is refused at the form, not minted.

        A blank description hashes to an intent nothing can match; a zero budget and an
        empty scope set both mint a token that can do nothing, which reads as a broken
        system rather than a refused request.
        """
        with pytest.raises(TaskRegistryError):
            registry.create_task(
                description=description,
                budget=budget,
                scopes=scopes,
                principal_id="kc:alice",
            )


class TestSpawnAgent:
    """`authority(child) <= authority(parent)`, enforced where the operator can see it."""

    def test_a_child_gets_a_strictly_narrower_token(
        self, registry: TaskRegistry, key_set: RootKeySet
    ) -> None:
        task = registry.create_task(
            description="Procure 500 units of packaging stock.",
            budget=Decimal("50000"),
            scopes=frozenset({"invoice:read", "payment:initiate"}),
            principal_id="kc:alice",
        )
        child = registry.spawn_agent(
            task_id=task.task_id,
            parent_id="root",
            agent_id="agt-payer",
            role="payer",
            scopes=frozenset({"payment:initiate"}),
            ceiling=Decimal("20000"),
        )
        assert child.depth == 1
        assert child.parent_id == "root"
        assert child.scopes == {"payment:initiate"}
        # The child is a real, verifiable token, not a bookkeeping entry.
        verify(child.token, key_set, now=_NOW)

    def test_a_grandchild_is_narrower_again(self, registry: TaskRegistry) -> None:
        """Depth is what the Cedar rule about `principal.depth` acts on, so it has to be real."""
        task = registry.create_task(
            description="t",
            budget=Decimal("50000"),
            scopes=frozenset({"payment:initiate"}),
            principal_id="kc:alice",
        )
        registry.spawn_agent(
            task_id=task.task_id,
            parent_id="root",
            agent_id="agt-payer",
            role="payer",
            scopes=frozenset({"payment:initiate"}),
            ceiling=Decimal("20000"),
        )
        grandchild = registry.spawn_agent(
            task_id=task.task_id,
            parent_id="agt-payer",
            agent_id="agt-settlement",
            role="payer",
            scopes=frozenset({"payment:initiate"}),
            ceiling=Decimal("5000"),
        )
        assert grandchild.depth == 2

    def test_a_scope_the_parent_never_held_is_refused(self, registry: TaskRegistry) -> None:
        """The headline guarantee, at the one surface an operator drives by hand.

        `attenuate` refuses this itself; asserting it here is about the page surfacing
        the refusal rather than minting a token the PEP would later reject, which would
        move the failure somewhere far less legible.
        """
        task = registry.create_task(
            description="t",
            budget=Decimal("50000"),
            scopes=frozenset({"invoice:read"}),
            principal_id="kc:alice",
        )
        with pytest.raises(TaskRegistryError):
            registry.spawn_agent(
                task_id=task.task_id,
                parent_id="root",
                agent_id="agt-payer",
                role="payer",
                scopes=frozenset({"payment:initiate"}),
                ceiling=Decimal("1000"),
            )

    def test_a_duplicate_name_is_refused(self, registry: TaskRegistry) -> None:
        """Two agents sharing a name would collapse into one node on the tree."""
        task = registry.create_task(
            description="t",
            budget=Decimal("50000"),
            scopes=frozenset({"payment:initiate"}),
            principal_id="kc:alice",
        )
        for _ in range(1):
            registry.spawn_agent(
                task_id=task.task_id,
                parent_id="root",
                agent_id="agt-payer",
                role="payer",
                scopes=frozenset({"payment:initiate"}),
                ceiling=Decimal("20000"),
            )
        with pytest.raises(TaskRegistryError):
            registry.spawn_agent(
                task_id=task.task_id,
                parent_id="root",
                agent_id="agt-payer",
                role="payer",
                scopes=frozenset({"payment:initiate"}),
                ceiling=Decimal("20000"),
            )

    def test_an_unknown_task_or_parent_is_refused(self, registry: TaskRegistry) -> None:
        import uuid

        with pytest.raises(TaskRegistryError):
            registry.spawn_agent(
                task_id=uuid.uuid4(),
                parent_id="root",
                agent_id="agt-payer",
                role="payer",
                scopes=frozenset({"payment:initiate"}),
                ceiling=Decimal("1"),
            )

        task = registry.create_task(
            description="t",
            budget=Decimal("100"),
            scopes=frozenset({"payment:initiate"}),
            principal_id="kc:alice",
        )
        with pytest.raises(TaskRegistryError):
            registry.spawn_agent(
                task_id=task.task_id,
                parent_id="agt-nobody",
                agent_id="agt-payer",
                role="payer",
                scopes=frozenset({"payment:initiate"}),
                ceiling=Decimal("1"),
            )


class TestTokenHandling:
    """Tokens are held for the driver, and not handed to the browser."""

    def test_the_json_view_carries_no_token(self, registry: TaskRegistry) -> None:
        """`as_dict` feeds a template. A token in it would end up in page source."""
        task = registry.create_task(
            description="t",
            budget=Decimal("100"),
            scopes=frozenset({"invoice:read"}),
            principal_id="kc:alice",
        )
        rendered = str(task.as_dict())
        assert task.root.token not in rendered
        assert "token" not in rendered

    def test_token_for_names_what_is_missing(self, registry: TaskRegistry) -> None:
        task = registry.create_task(
            description="t",
            budget=Decimal("100"),
            scopes=frozenset({"invoice:read"}),
            principal_id="kc:alice",
        )
        assert registry.token_for(task.task_id, "root") == task.root.token
        with pytest.raises(TaskRegistryError):
            registry.token_for(task.task_id, "agt-nobody")
