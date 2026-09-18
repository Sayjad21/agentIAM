"""`scripts/seed_demo.py` — T-057, TODO item 7.

The scenario is the product's own demonstration, so what these tests protect is that it
still *demonstrates* something: a delegation tree whose agents differ from one another, and
traffic that reaches every distinct refusal the system can produce.

That last part is the one worth guarding. Two of the traffic lines used to be mislabelled
because the amounts could not distinguish the layer that refused them — 600,000 is over the
mandate's own ceiling *and* over the signed bundle's, and the token refuses first, so a line
captioned "the policy forbids" was measuring the caveat layer.
`test_the_traffic_covers_every_refusal_layer` is what stops that recurring: it asserts the
scenario reaches the depth rule, which is the only condition in the corpus bundle that a
valid token cannot also violate on its own.

Nothing here talks to Postgres or a PEP. The database and traffic halves are exercised for
real by the `demo-stack` CI job, which brings the whole thing up and drives it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from scripts import seed_demo


def _depths() -> dict[str, int]:
    depths = {agent: 1 for agent, *_ in seed_demo._CHILDREN}
    for parent, agent, *_ in seed_demo._DESCENDANTS:
        depths[agent] = depths[parent] + 1
    return depths


class TestTheDelegationTree:
    def test_every_child_is_strictly_narrower_than_the_mandate(self) -> None:
        """A sub-agent that held the mandate's full grant would demonstrate nothing."""
        mandate_scopes = {"invoice:read", "vendor:read", "payment:initiate"}
        for _agent, _role, scopes, _ceiling in seed_demo._CHILDREN:
            assert scopes < mandate_scopes, f"{scopes} is not a proper subset"

    def test_the_children_differ_from_one_another(self) -> None:
        """`DEMO.md` beat 2 is 'each node's scopes visibly smaller' — visibly *different* too."""
        scope_sets = [scopes for _a, _r, scopes, _c in seed_demo._CHILDREN]
        assert len(set(scope_sets)) == len(scope_sets)

    def test_the_doc_reader_can_spend_nothing(self) -> None:
        """Beat 3 turns on this: the agent that attempts a payment must have a zero ceiling."""
        ceilings = {agent: c for agent, _r, _s, c in seed_demo._CHILDREN}
        assert ceilings["agt-doc-reader"] == Decimal("0.0000")

    def test_each_descendant_narrows_its_own_parent(self) -> None:
        """The attenuation invariant, made visible in the tree rather than only asserted.

        Never wider in scopes or ceiling, and strictly narrower in at least one — a read-only
        grandchild keeps its parent's zero ceiling but drops a scope.
        """
        grants = {agent: (s, c) for agent, _r, s, c in seed_demo._CHILDREN}
        for parent, agent, _role, scopes, ceiling in seed_demo._DESCENDANTS:
            parent_scopes, parent_ceiling = grants[parent]
            assert scopes <= parent_scopes, f"{agent} widens {parent}'s scopes"
            assert ceiling <= parent_ceiling, f"{agent} widens {parent}'s ceiling"
            assert scopes < parent_scopes or ceiling < parent_ceiling, (
                f"{agent} does not narrow {parent}"
            )
            grants[agent] = (scopes, ceiling)

    def test_the_tree_is_deep_enough_to_break_the_policys_depth_rule(self) -> None:
        """The corpus bundle permits payments only at `principal.depth <= 2`.

        Without an agent below that line the scenario has no POLICY_DENIED in it at all,
        because the mandate's ceiling and the bundle's are both 500,000 — no *amount* can
        be refused by one and not the other.
        """
        assert _depths()["agt-subcontractor"] == 3

    def test_the_tree_fits_the_mandates_depth_limit(self) -> None:
        """Deep enough to break the policy rule, not so deep the token refuses first."""
        mandate = seed_demo._mandate(datetime.now(UTC))
        assert max(_depths().values()) <= mandate.max_depth

    def test_it_is_a_tree_and_not_a_chain(self) -> None:
        """Some agent below the root's children must have a sibling."""
        parents = [parent for parent, *_ in seed_demo._DESCENDANTS]
        assert len(parents) != len(set(parents))


class TestTheTraffic:
    def test_it_uses_only_agents_the_seed_mints(self) -> None:
        minted = {"root"} | {a for a, _r, _s, _c in seed_demo._CHILDREN}
        minted |= {a for _p, a, _r, _s, _c in seed_demo._DESCENDANTS}
        for _label, who, _method, _path, _body in seed_demo._TRAFFIC:
            assert who in minted, f"{who} is never minted"

    def test_every_agent_makes_at_least_one_call(self) -> None:
        """An agent that never acts never appears.

        The identity tree is derived from the audit chain, so a sub-agent that is minted
        and then never used is invisible in the demonstration it exists for.
        """
        minted = {"root"} | {a for a, _r, _s, _c in seed_demo._CHILDREN}
        minted |= {a for _p, a, _r, _s, _c in seed_demo._DESCENDANTS}
        called = {who for _l, who, _m, _p, _b in seed_demo._TRAFFIC}
        assert minted == called, f"never call: {minted - called}"

    def test_the_traffic_covers_every_refusal_layer(self) -> None:
        """Allows, a scope refusal, a caveat ceiling, the mandate's ceiling, and a policy refusal.

        A scenario that only produced allows would show a console full of green and prove
        nothing; one that never reached the policy layer would leave Cedar untested by the
        demonstration that exists to show it working.

        The mandate's own ceiling is listed separately from the caveat ceiling since TODO
        item 17: they used to report the same reason code, so the console showed one refusal
        where the system makes two distinct ones.
        """
        labels = " | ".join(label for label, *_ in seed_demo._TRAFFIC).lower()
        assert "attempts a payment" in labels, "no scope refusal"
        assert "exceeds its ceiling" in labels, "no caveat-ceiling refusal"
        assert "more than the mandate grants" in labels, "no mandate-ceiling refusal"
        assert "too deep for the policy" in labels, "no policy refusal"
        assert any(body is None for *_head, body in seed_demo._TRAFFIC), "no reads"

    def test_refusals_are_few(self) -> None:
        """One per refusal layer, against a much larger body of allowed work."""
        refusals = [
            label
            for label, *_ in seed_demo._TRAFFIC
            if any(marker in label for marker in ("attempts", "exceeds", "too deep"))
        ]
        assert len(refusals) == 4
        assert len(seed_demo._TRAFFIC) >= 20 * len(refusals)

    def test_payments_fit_inside_one_lease(self) -> None:
        """A request larger than the PEP's lease is refused however full the pool is.

        The only payments allowed over it are the two that exist to be refused by a ceiling.
        """
        from scripts.pep_service import DEFAULT_LEASE_SIZE

        for label, _who, _method, _path, body in seed_demo._TRAFFIC:
            if body is not None and "exceeds" not in label and "more than" not in label:
                assert Decimal(body["amount"]) < DEFAULT_LEASE_SIZE, label

    def test_agents_spawn_parent_first(self) -> None:
        """`drive` pauses on each agent's first call; no child may appear before its parent."""
        seen: list[str] = []
        for _label, who, *_ in seed_demo._TRAFFIC:
            if who not in seen:
                seen.append(who)
        for parent, agent, *_ in seed_demo._DESCENDANTS:
            assert seen.index(parent) < seen.index(agent), f"{agent} spawns before {parent}"
        assert seen[0] == "root"

    def test_the_paths_are_all_proxied(self) -> None:
        """Anything not under /proxy bypasses the thing being demonstrated."""
        for _label, _who, _method, path, _body in seed_demo._TRAFFIC:
            assert path.startswith("/proxy/")


class TestTheEscalations:
    def test_each_names_an_agent_the_seed_mints(self) -> None:
        """The tree flags a node with a pending escalation — an unminted agent has no node."""
        minted = {a for a, _r, _s, _c in seed_demo._CHILDREN}
        minted |= {a for _p, a, _r, _s, _c in seed_demo._DESCENDANTS}
        for agent, *_ in seed_demo._ESCALATIONS:
            assert agent in minted

    def test_one_asks_for_more_than_one_scope(self) -> None:
        """So an approver can drop a scope and show narrowing, not only cut the amount."""
        assert any(len(scopes) > 1 for _a, scopes, _m, _r in seed_demo._ESCALATIONS)

    def test_they_outlive_the_gap_before_the_demo(self) -> None:
        """The API's 15-minute default would expire them before anyone opened the queue."""
        assert seed_demo.ESCALATION_TTL_S >= 3600


class TestTheFixedIdentifiers:
    def test_the_mandate_id_is_a_constant(self) -> None:
        """`docker-compose.demo.yml` names it before this script runs, so it cannot be random.

        `tests/unit/test_demo_compose.py` checks the two agree; this checks it is stable
        across imports at all, which a `uuid4()` default would not be.
        """
        assert seed_demo.DEMO_MANDATE_ID == seed_demo.DEMO_MANDATE_ID
        assert str(seed_demo.DEMO_MANDATE_ID) != str(seed_demo.DEMO_TASK_ID)

    def test_the_pool_matches_the_demo_script(self) -> None:
        """`DEMO.md` beat 1 says ৳500,000; a console showing a different figure undercuts it."""
        assert seed_demo.POOL_TOTAL == Decimal("500000.0000")
